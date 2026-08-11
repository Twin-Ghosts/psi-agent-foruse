#!/usr/bin/env python3
"""
阿里云 RPC V1.0 签名机制 —— 官方测试向量验证脚本
===================================================
对照阿里云官方文档《V2版本RPC风格API请求的请求体与签名机制》中
"固定参数示例"（DescribeDedicatedHosts / ECS）给出的期望签名值，
验证 percent_encode + stringToSign + HMAC-SHA1 整条链路。

官方文档地址：
  https://help.aliyun.com/zh/sdk/product-overview/rpc-mechanism

官方固定参数（AccessKeyId=testid / AccessKeySecret=testsecret）：
  期望签名 = 9NaGiOspFP5UPcwX8Iwt2YJXXuk=

只依赖 Python 标准库，可离线运行。
"""

import base64
import hashlib
import hmac
import urllib.parse
from collections import OrderedDict

# ---- 官方固定参数示例（摘自阿里云文档） ----
OFFICIAL_CASE = {
    "AccessKeyId": "testid",
    "Action": "DescribeDedicatedHosts",
    "Format": "JSON",
    "RegionId": "cn-beijing",
    "SignatureMethod": "HMAC-SHA1",
    "SignatureNonce": "edb2b34af0af9a6d14deaf7c1a5315eb",
    "SignatureVersion": "1.0",
    "Timestamp": "2023-03-13T08:34:30Z",
    "Version": "2014-05-26",
}
OFFICIAL_ACCESS_KEY_SECRET = "testsecret"
OFFICIAL_EXPECTED_SIGNATURE = "9NaGiOspFP5UPcwX8Iwt2YJXXuk="
OFFICIAL_HTTP_METHOD = "GET"


def percent_encode(s: str) -> str:
    """
    RFC 3986 编码，与阿里云官方 Python 示例一致：
      1. urllib.parse.quote(..., safe=b"~")  —— 注意 safe 不含 "/"，
         因此 "/" 也会被编码为 %2F（RPC 机制要求）；
      2. "+" -> "%20"；
      3. "*" -> "%2A"。
    """
    encoded = urllib.parse.quote(s.encode("utf-8"), safe=b"~")
    return encoded.replace("+", "%20").replace("*", "%2A")


def generate_signature(access_key_secret: str, string_to_sign: str) -> str:
    """HMAC-SHA1，key = AccessKeySecret + '&'，结果 base64。"""
    signing_key = (access_key_secret + "&").encode("utf-8")
    digest = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def sign_rpc_request(http_method: str, params: dict, access_key_secret: str) -> str:
    """完整 RPC V1.0 签名：排序 -> 规范化 -> stringToSign -> HMAC-SHA1。"""
    # 1. 按参数名 ASCII 码升序排序
    sorted_params = OrderedDict(sorted(params.items()))
    # 2. 规范化请求字符串：percentEncode(k)=percentEncode(v)，& 连接
    canonical_query_string = "&".join(
        f"{percent_encode(k)}={percent_encode(str(v))}"
        for k, v in sorted_params.items()
    )
    # 3. stringToSign = HTTPMethod & %2F & percentEncode(canonicalQueryString)
    string_to_sign = (
        f"{http_method.upper()}&{percent_encode('/')}&{percent_encode(canonical_query_string)}"
    )
    # 4. 签名
    return generate_signature(access_key_secret, string_to_sign)


def main() -> None:
    signature = sign_rpc_request(OFFICIAL_HTTP_METHOD, OFFICIAL_CASE, OFFICIAL_ACCESS_KEY_SECRET)
    print(f"计算签名: {signature}")
    print(f"期望签名: {OFFICIAL_EXPECTED_SIGNATURE}")
    if signature == OFFICIAL_EXPECTED_SIGNATURE:
        print("✅ PASS —— 与阿里云官方测试向量完全一致，签名链路正确。")
        return
    print("❌ FAIL —— 与官方向量不一致，请对照文档逐项排查。")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
