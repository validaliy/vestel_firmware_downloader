#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 Vestel Smart TV - Canlı Firmware Keşif ve İndirme Motoru
=============================================================================
Bu betik, AWS S3 Signature v4 protokolünü kullanarak Vestel'in resmi sunucularını
(vstlupdate / qaupdate) otomatik olarak tarar, aktif ve indirilebilir olan tüm
firmware paketlerini numaralı bir liste halinde kullanıcıya sunar.

TEMEL KAVRAMLAR:
* DCF (Device Configuration File):
  Televizyonun donanım/panel kimliğini ve yazılımın parça haritasını (reçetesini)
  tutan yapılandırma dosyasıdır (örn: 7800.dcf). Yazılımın kaç parçadan oluştuğunu
  (part_1...part_17), SHA-256 hash'lerini ve flash adreslerini belirtir.
* Havuz (Bucket):
  Dosyaların saklandığı AWS S3 bulut depolarıdır.
  - vstlupdate: Son kullanıcı televizyonlarının bağlandığı resmi kararlı havuz.
  - qaupdate: Vestel mühendislerinin yeni yazılımları test ettiği QA test havuzu.
* Marka / Ülke Bilgisi Neden Yok?
  S3 dosya yolu donanım odaklıdır ({BOARD}/{DCF}/0009df/{DCF}.dcf). Marka (Toshiba,
  Vestel, JVC) ve ülke bilgileri sadece arayüz changelog metinleri için kullanılır;
  aynı anakart ve DCF'e sahip tüm TV'lerin ikili yazılım imajı aynıdır.
"""

import os
import sys
import time
import argparse
import datetime
import hashlib
import hmac
import urllib.request
import urllib.error
import concurrent.futures

# aurora.elf içinden decompile edilen IAM anahtarları (XOR korumalı)
def _dec(b_list, k=0x42) -> str:
    return "".join(chr(b ^ k) for b in b_list)

AWS_CREDENTIALS = {
    "MB180": {
        "access_key": _dec([3, 9, 11, 3, 16, 18, 21, 7, 112, 113, 15, 118, 1, 117, 3, 23, 112, 27, 27, 9]),
        "secret_key": _dec([9, 113, 123, 13, 8, 16, 16, 4, 9, 54, 59, 40, 105, 24, 116, 46, 47, 20, 45, 0, 22, 52, 46, 6, 44, 24, 59, 105, 15, 112, 51, 13, 53, 40, 116, 1, 35, 21, 122, 117]),
    },
    "MB181": {
        "access_key": _dec([3, 9, 11, 3, 16, 18, 21, 7, 112, 113, 15, 118, 1, 5, 12, 4, 7, 113, 14, 3]),
        "secret_key": _dec([115, 123, 58, 14, 48, 47, 46, 118, 21, 112, 38, 43, 118, 13, 105, 21, 18, 11, 32, 36, 39, 59, 0, 6, 105, 59, 46, 53, 0, 15, 19, 53, 24, 55, 50, 33, 8, 114, 45, 41]),
    }
}

DEFAULT_REGION = "eu-west-1"
DEFAULT_OUI = "0009df"

# Otomatik taranacak yaygın profil kataloğu
SCAN_CATALOG = [
    ("MB181", "7800", "vstlupdate"),
    ("MB181", "7801", "vstlupdate"),
    ("MB181", "7800", "qaupdate"),
    ("MB181", "7801", "qaupdate"),
    ("MB181", "7600", "vstlupdate"),
    ("MB181", "7400", "vstlupdate"),
    ("MB180", "7800", "vstlupdate"),
    ("MB180", "7600", "vstlupdate"),
    ("17MB181", "7800", "vstlupdate"),
    ("MB130", "7800", "vstlupdate"),
]

def sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

def get_signature_key(key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
    k_date = sign(("AWS4" + key).encode("utf-8"), date_stamp)
    k_region = sign(k_date, region_name)
    k_service = sign(k_region, service_name)
    k_signing = sign(k_service, "aws4_request")
    return k_signing

class VestelFirmwareDownloader:
    def __init__(self, board="MB181", dcf="7800", oui=DEFAULT_OUI,
                 bucket="vstlupdate", region=DEFAULT_REGION, output_dir="firmware_output"):
        self.board = board.strip()
        self.dcf = dcf.strip()
        self.oui = oui.strip()
        self.bucket = bucket.strip()
        self.region = region.strip()
        self.output_dir = output_dir.strip()

        creds = AWS_CREDENTIALS.get(self.board, AWS_CREDENTIALS["MB181"])
        self.access_key = creds["access_key"]
        self.secret_key = creds["secret_key"]
        os.makedirs(self.output_dir, exist_ok=True)

    def _create_authorized_request(self, s3_key: str, method: str = "GET") -> urllib.request.Request:
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = f"{self.bucket}.s3.{self.region}.amazonaws.com"
        canonical_uri = "/" + s3_key.lstrip("/")

        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:UNSIGNED-PAYLOAD\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        payload_hash = "UNSIGNED-PAYLOAD"

        canonical_request = (
            f"{method}\n"
            f"{canonical_uri}\n\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )
        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = (
            f"{algorithm}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        signing_key = get_signature_key(self.secret_key, date_stamp, self.region, "s3")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        auth_header = (
            f"{algorithm} Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        url = f"https://{host}{canonical_uri}"
        req = urllib.request.Request(url, method=method, headers={
            "x-amz-date": amz_date,
            "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
            "Authorization": auth_header,
            "User-Agent": f"UID(00:00:00:00:00:00/{self.board}/VESTEL/65)+CE-HTML"
        })
        return req

    def fetch_dcf_data(self):
        s3_key = f"{self.board}/{self.dcf}/{self.oui}/{self.dcf}.dcf"
        req = self._create_authorized_request(s3_key, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                return data, None
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}"
        except Exception as e:
            return None, str(e)

    def parse_dcf(self, data_bytes: bytes):
        content = data_bytes.decode("latin1", errors="ignore")
        parts = []
        target_version = "Bilinmiyor"
        reasons = []

        current = {}
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("Version=") and target_version == "Bilinmiyor":
                target_version = line.split("=", 1)[1].strip()
            elif line.startswith("LangReason="):
                reasons.append(line.split("=", 1)[1].strip())
            elif line.startswith("Load="):
                if current and "load" in current:
                    parts.append(current)
                current = {"load": line.split("=", 1)[1].strip()}
            elif line.startswith("Authentication=") and current:
                current["auth"] = line.split("=", 1)[1].strip()
            elif line.startswith("Target=") and current:
                current["target"] = line.split("=", 1)[1].strip()
            elif line.startswith("SourceEnd=") and current:
                current["source_end"] = line.split("=", 1)[1].strip()

        if current and "load" in current:
            parts.append(current)

        total_bytes = sum(int(p.get("source_end", 19999999)) + 1 for p in parts)
        return {
            "version": target_version,
            "reasons": reasons,
            "parts": parts,
            "total_bytes": total_bytes
        }

    def download_part(self, filename: str, expected_hash: str) -> str:
        s3_key = f"{self.board}/{self.dcf}/{self.oui}/images/{filename}"
        dest_path = os.path.join(self.output_dir, filename)

        if os.path.exists(dest_path):
            with open(dest_path, "rb") as f:
                data = f.read()
            if hashlib.sha256(data).hexdigest().lower() == expected_hash.lower():
                print(f"  [+] [MEVCUT] {filename} doğrulandı (atlandı).")
                return dest_path

        req = self._create_authorized_request(s3_key, method="GET")
        t0 = time.time()
        with urllib.request.urlopen(req) as resp:
            downloaded = 0
            hasher = hashlib.sha256()

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)

        dt = time.time() - t0
        calc_hash = hasher.hexdigest().lower()

        if calc_hash != expected_hash.lower():
            print(f"  [-] [HATA] {filename} SHA256 uyuşmazlığı!")
            if os.path.exists(dest_path):
                os.remove(dest_path)
            sys.exit(1)

        speed_mb = (downloaded / dt / (1024 * 1024)) if dt > 0 else 0
        print(f"  [+] [TAMAM] {filename} ({downloaded / (1024*1024):.2f} MB, {speed_mb:.2f} MB/s) [SHA256: OK]")
        return dest_path

    def assemble(self, parts, output_filename="aurora.upg") -> str:
        final_path = os.path.join(self.output_dir, output_filename)
        print(f"\n[*] Parçalar birleştiriliyor -> {final_path}")

        total_bytes = 0
        hasher = hashlib.sha256()
        with open(final_path, "wb") as out_f:
            for p in parts:
                fname = os.path.basename(p["load"])
                fpath = os.path.join(self.output_dir, fname)
                with open(fpath, "rb") as in_f:
                    while True:
                        buf = in_f.read(1024 * 1024)
                        if not buf:
                            break
                        out_f.write(buf)
                        hasher.update(buf)
                        total_bytes += len(buf)

        print(f"[+] Firmware başarıyla birleştirildi!")
        print(f"    Dosya     : {final_path}")
        print(f"    Boyut     : {total_bytes} bayt ({total_bytes / (1024*1024):.2f} MB)")
        print(f"    SHA-256   : {hasher.hexdigest()}")
        return final_path

    def execute_download(self, dcf_data=None):
        if dcf_data is None:
            dcf_data, err = self.fetch_dcf_data()
            if err:
                print(f"[-] Hata: {err}")
                return False

        # DCF dosyasını kaydet
        local_dcf = os.path.join(self.output_dir, f"{self.dcf}.dcf")
        with open(local_dcf, "wb") as f:
            f.write(dcf_data)

        info = self.parse_dcf(dcf_data)
        parts = info["parts"]

        print(f"\n[*] Firmware indirmesi başlatılıyor ({len(parts)} parça, ~{info['total_bytes'] / (1024*1024):.2f} MB)...")
        for idx, p in enumerate(parts, 1):
            fname = os.path.basename(p["load"])
            print(f"[{idx:02d}/{len(parts):02d}] {fname} indiriliyor...")
            self.download_part(fname, p["auth"])

        self.assemble(parts)
        print("\n" + "=" * 65)
        print("  GÜNCELLEME İNDİRME VE BİRLEŞTİRME BAŞARIYLA BİTTİ!")
        print("=" * 65)
        return True

def probe_single_target(board, dcf, bucket):
    downloader = VestelFirmwareDownloader(board=board, dcf=dcf, bucket=bucket)
    data, err = downloader.fetch_dcf_data()
    if data:
        info = downloader.parse_dcf(data)
        return {
            "board": board,
            "dcf": dcf,
            "bucket": bucket,
            "version": info["version"],
            "parts_count": len(info["parts"]),
            "total_bytes": info["total_bytes"],
            "reasons": info["reasons"],
            "dcf_data": data,
            "downloader": downloader
        }
    return None

def scan_and_select_firmware():
    print("\n" + "=" * 70)
    print("      VESTEL FIRMWARE DOWNLOADER - OTOMATİK KEŞİF & SEÇİM MENÜSÜ")
    print("=" * 70)
    print("[*] Vestel AWS S3 depoları anlık taranıyor (MB181, MB180, DCF profilleri)...")

    found_targets = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(probe_single_target, b, d, bk) for b, d, bk in SCAN_CATALOG]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res:
                found_targets.append(res)

    # Sıralama: Board ve DCF'e göre
    found_targets.sort(key=lambda x: (x["board"], x["dcf"], x["bucket"]))

    if not found_targets:
        print("[-] Sunucuda aktif standart profil bulunamadı.")
        return

    print(f"\n[✓] Tarama tamamlandı! Bulunan aktif ve indirilebilir firmware paketleri:\n")
    print(f" {'No':<4} | {'Anakart':<8} | {'DCF':<6} | {'Havuz':<11} | {'Sürüm Kodu':<10} | {'Boyut':<10} | {'Açıklama'}")
    print("-" * 75)

    for idx, t in enumerate(found_targets, 1):
        size_mb = f"{t['total_bytes'] / (1024*1024):.1f} MB"
        desc = t["reasons"][0] if t["reasons"] else "-"
        if len(desc) > 22:
            desc = desc[:20] + ".."
        print(f" [{idx:02d}] | {t['board']:<8} | {t['dcf']:<6} | {t['bucket']:<11} | {t['version']:<10} | {size_mb:<10} | {desc}")

    print("\n [M]  | Manuel / Özel Parametre Gir (Özel DCF / Anakart)")
    print(" [Q]  | Çıkış\n")

    while True:
        choice = input("Lütfen indirmek istediğiniz paketin numarasını giriniz [1-%d]: " % len(found_targets)).strip()
        if choice.lower() in ["q", "exit", "quit"]:
            print("[!] Çıkış yapıldı.")
            return
        elif choice.lower() == "m":
            run_manual_wizard()
            return
        elif choice.isdigit() and 1 <= int(choice) <= len(found_targets):
            selected = found_targets[int(choice) - 1]
            print("\n" + "=" * 65)
            print("  SEÇİLEN FIRMWARE PAKETİ:")
            print(f"  Anakart : {selected['board']} | DCF: {selected['dcf']} | Havuz: {selected['bucket']}")
            print(f"  Sürüm   : {selected['version']} | Boyut: {selected['total_bytes'] / (1024*1024):.2f} MB")
            print("=" * 65)

            confirm = input("\nİndirme ve birleştirme işlemini başlatmak istiyor musunuz? [E/h]: ").strip().lower()
            if confirm in ["", "e", "evet", "y", "yes"]:
                selected["downloader"].execute_download(dcf_data=selected["dcf_data"])
            else:
                print("[!] İptal edildi.")
            return
        else:
            print("Geçersiz seçim. Lütfen listedeki bir numarayı veya M/Q giriniz.")

def run_manual_wizard():
    print("\n--- MANUEL GİRİŞ SİHİRBAZI ---")
    board = input("Anakart modeli [Varsayılan: MB181]: ").strip() or "MB181"
    dcf = input("DCF kodu [Varsayılan: 7800]: ").strip() or "7800"
    bucket = input("Havuz (vstlupdate / qaupdate) [Varsayılan: vstlupdate]: ").strip() or "vstlupdate"
    out_dir = input("Kayıt klasörü [Varsayılan: firmware_output]: ").strip() or "firmware_output"

    downloader = VestelFirmwareDownloader(board=board, dcf=dcf, bucket=bucket, output_dir=out_dir)
    data, err = downloader.fetch_dcf_data()
    if err:
        print(f"[-] Profil sunucuda bulunamadı: {err}")
        return
    downloader.execute_download(dcf_data=data)

def main():
    parser = argparse.ArgumentParser(description="Vestel Canlı Firmware İndirme Aracı")
    parser.add_argument("--scan", action="store_true", help="Otomatik tarayıcı ve seçim menüsünü aç")
    parser.add_argument("--board", default=None, help="Anakart modeli (örn: MB181, MB180)")
    parser.add_argument("--dcf", default=None, help="DCF kodu (Panel/donanım kimliği ve parça reçetesi, örn: 7800, 7801)")
    parser.add_argument("--bucket", default="vstlupdate", help="AWS S3 havuzu (vstlupdate: resmi dağıtım havuzu, qaupdate: test havuzu)")
    parser.add_argument("--output", default="firmware_output", help="Kayıt dizini")

    args = parser.parse_args()

    # Hiçbir CLI argümanı verilmediyse veya --scan istendiyse otomatik tarama ve seçim menüsünü aç
    if args.scan or (args.board is None and args.dcf is None and len(sys.argv) == 1):
        scan_and_select_firmware()
    else:
        board = args.board if args.board else "MB181"
        dcf = args.dcf if args.dcf else "7800"
        downloader = VestelFirmwareDownloader(
            board=board,
            dcf=dcf,
            bucket=args.bucket,
            output_dir=args.output
        )
        downloader.execute_download()

if __name__ == "__main__":
    main()
