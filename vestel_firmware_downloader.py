#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 Vestel MB181 / MediaTek - Canlı Firmware İndirme ve Birleştirme Motoru
=============================================================================
Bu betik, Vestel 17MB181 TV anakartının `aurora.elf` middleware yazılımından
tersine mühendislik ile elde edilen AWS S3 Signature v4 kimlik doğrulama
algoritması ve DCF dosya ayrıştırma mantığını kullanarak en son firmware
paketini doğrudan Vestel'in resmi AWS S3 deposundan indirir ve birleştirir.
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

# aurora.elf (0x00e24cd8 - 0x00e24d1c) ofsetlerinden decompile edilen IAM kimlikleri (XOR korumalı)
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
DEFAULT_BUCKET = "vstlupdate"
DEFAULT_BOARD = "MB181"
DEFAULT_DCF = "7800"
DEFAULT_OUI = "0009df"

def sign(key: bytes, msg: str) -> bytes:
    """HMAC-SHA256 imza üretir."""
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

def get_signature_key(key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
    """AWS Signature v4 hiyerarşik türetilmiş imza anahtarını hesaplar."""
    k_date = sign(("AWS4" + key).encode("utf-8"), date_stamp)
    k_region = sign(k_date, region_name)
    k_service = sign(k_region, service_name)
    k_signing = sign(k_service, "aws4_request")
    return k_signing

class VestelFirmwareDownloader:
    def __init__(self, board=DEFAULT_BOARD, dcf=DEFAULT_DCF, oui=DEFAULT_OUI,
                 bucket=DEFAULT_BUCKET, region=DEFAULT_REGION, output_dir="firmware_output"):
        self.board = board
        self.dcf = dcf
        self.oui = oui
        self.bucket = bucket
        self.region = region
        self.output_dir = output_dir

        creds = AWS_CREDENTIALS.get(board, AWS_CREDENTIALS["MB181"])
        self.access_key = creds["access_key"]
        self.secret_key = creds["secret_key"]

        os.makedirs(self.output_dir, exist_ok=True)

    def _create_authorized_request(self, s3_key: str, method: str = "GET") -> urllib.request.Request:
        """Belirtilen S3 nesnesi için AWS SigV4 yetkilendirme başlıkları üretir."""
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

    def download_dcf(self) -> str:
        """S3 üzerinden hedef DCF yapılandırma dosyasını indirir."""
        s3_key = f"{self.board}/{self.dcf}/{self.oui}/{self.dcf}.dcf"
        local_path = os.path.join(self.output_dir, f"{self.dcf}.dcf")

        print(f"[*] DCF dosyası sorgulanıyor: s3://{self.bucket}/{s3_key}")
        req = self._create_authorized_request(s3_key, method="GET")

        try:
            with urllib.request.urlopen(req) as resp:
                data = resp.read()
                with open(local_path, "wb") as f:
                    f.write(data)
            print(f"[+] DCF dosyası başarıyla indirildi: {local_path} ({len(data)} bayt)")
            return local_path
        except urllib.error.HTTPError as e:
            print(f"[-] DCF indirme hatası: HTTP {e.code} - {e.reason}")
            sys.exit(1)

    def parse_dcf(self, dcf_path: str):
        """DCF dosyasından parça dosya yollarını, hash değerlerini ve flash adreslerini ayıklar."""
        parts = []
        with open(dcf_path, "rb") as f:
            content = f.read().decode("latin1", errors="ignore")

        current = {}
        target_version = None
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("Version=") and not target_version:
                target_version = line.split("=", 1)[1].strip()
            elif line.startswith("Load="):
                if current and "load" in current:
                    parts.append(current)
                current = {"load": line.split("=", 1)[1].strip()}
            elif line.startswith("Authentication=") and current:
                current["auth"] = line.split("=", 1)[1].strip()
            elif line.startswith("Target=") and current:
                current["target"] = line.split("=", 1)[1].strip()

        if current and "load" in current:
            parts.append(current)

        print(f"[i] DCF Sürüm Kodu : {target_version}")
        print(f"[i] Tespit Edilen Parça Sayısı : {len(parts)}")
        return parts

    def download_part(self, filename: str, expected_hash: str) -> str:
        """Tek bir firmware parçasını akış (stream) olarak indirir ve SHA256 doğrulaması yapar."""
        s3_key = f"{self.board}/{self.dcf}/{self.oui}/images/{filename}"
        dest_path = os.path.join(self.output_dir, filename)

        # Daha önce indirilmişse hash kontrolü yap
        if os.path.exists(dest_path):
            with open(dest_path, "rb") as f:
                data = f.read()
            if hashlib.sha256(data).hexdigest().lower() == expected_hash.lower():
                print(f"[+] [MEVCUT] {filename} doğrulandı (atlandı).")
                return dest_path

        req = self._create_authorized_request(s3_key, method="GET")
        t0 = time.time()
        with urllib.request.urlopen(req) as resp:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            hasher = hashlib.sha256()

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 512) # 512 KB bloklar
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)

        dt = time.time() - t0
        calc_hash = hasher.hexdigest().lower()

        if calc_hash != expected_hash.lower():
            print(f"[-] [HATA] {filename} SHA256 uyuşmazlığı!")
            print(f"    Hesaplanan: {calc_hash}")
            print(f"    Beklenen  : {expected_hash.lower()}")
            if os.path.exists(dest_path):
                os.remove(dest_path)
            sys.exit(1)

        speed_mb = (downloaded / dt / (1024 * 1024)) if dt > 0 else 0
        print(f"[+] [TAMAM] {filename} ({downloaded / (1024*1024):.2f} MB, {speed_mb:.2f} MB/s) [SHA256: OK]")
        return dest_path

    def assemble_firmware(self, parts, output_filename="aurora.upg") -> str:
        """Tüm doğrulanmış parçaları birleştirerek tam firmware dosyasını oluşturur."""
        final_path = os.path.join(self.output_dir, output_filename)
        print(f"\n[*] Parçalar birleştiriliyor -> {final_path}")

        total_bytes = 0
        hasher = hashlib.sha256()
        with open(final_path, "wb") as out_f:
            for idx, p in enumerate(parts, 1):
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
        print(f"    Hedef Yol : {final_path}")
        print(f"    Toplam Boyut: {total_bytes} bayt ({total_bytes / (1024*1024):.2f} MB)")
        print(f"    SHA-256   : {hasher.hexdigest()}")
        return final_path

    def run(self):
        print("=" * 70)
        print("  VESTEL FIRMWARE DOWNLOADER (CANLI AWS S3)")
        print("=" * 70)
        dcf_file = self.download_dcf()
        parts = self.parse_dcf(dcf_file)

        print("\n[*] Firmware parçaları indiriliyor...")
        for idx, p in enumerate(parts, 1):
            fname = os.path.basename(p["load"])
            print(f"[{idx:02d}/{len(parts):02d}] {fname} indiriliyor...")
            self.download_part(fname, p["auth"])

        self.assemble_firmware(parts)
        print("\n" + "=" * 70)
        print("  İŞLEM BAŞARIYLA TAMAMLANDI!")
        print("=" * 70)

def main():
    parser = argparse.ArgumentParser(description="Vestel Canlı Firmware İndirme Aracı")
    parser.add_argument("--board", default=DEFAULT_BOARD, help="Anakart modeli (Varsayılan: MB181)")
    parser.add_argument("--dcf", default=DEFAULT_DCF, help="DCF kodu (Varsayılan: 7800)")
    parser.add_argument("--oui", default=DEFAULT_OUI, help="OUI kodu (Varsayılan: 0009df)")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="AWS S3 Havuzu (Varsayılan: vstlupdate)")
    parser.add_argument("--output", default="firmware_output", help="Kayıt dizini")

    args = parser.parse_args()
    downloader = VestelFirmwareDownloader(
        board=args.board,
        dcf=args.dcf,
        oui=args.oui,
        bucket=args.bucket,
        output_dir=args.output
    )
    downloader.run()

if __name__ == "__main__":
    main()
