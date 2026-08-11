# -*- coding: utf-8 -*-
"""发码通道抽象。

第 3 步之前只有 MockProvider；第 5 步接 Resend、第 6 步接 PNVS 时新增实现类，
service.py 不改。

两条来自方案文档的关键差异：

    邮箱  Resend 只负责投递，验证码全生命周期由我们管（生成/存哈希/过期/试错）
    手机  PNVS 托管验证码，生成与校验都在阿里云侧，我们零验证码存储

所以接口分成 send_code / check_code 两个方法：邮箱通道的 check_code 由本地
状态机完成，手机通道的 check_code 要回调供应商。
"""

import hashlib
import hmac
import os
import secrets
import time

CODE_LENGTH = 6
CODE_TTL = 600          # 10 分钟，与方案文档一致
MAX_ATTEMPTS = 5

# 摘要盐。生产必须显式设置，否则进程重启后旧验证码全部失效。
CODE_SALT = os.environ.get("EMAIL_CODE_SALT", "")


def _salt():
    global CODE_SALT
    if not CODE_SALT:
        CODE_SALT = secrets.token_hex(16)
    return CODE_SALT.encode()


def generate_code(length=CODE_LENGTH):
    """密码学安全的纯数字验证码。允许首位为 0，靠定长比对而非数值比较。"""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_code(identifier, code):
    """连同 identifier 一起摘要，避免同码跨账号撞用。"""
    return hmac.new(_salt(), f"{identifier}:{code}".encode(),
                    hashlib.sha256).hexdigest()


class MockProvider:
    """本地 mock：不出网、不花钱、不发真信。

    记录调用次数，供契约测试断言"限频发生在调用供应商之前"。
    """

    name = "mock"

    def __init__(self):
        self.calls = 0
        self.sent = []          # [(identifier, code)]
        self.hosted = {}        # 模拟 PNVS 的托管验证码
        self.fail_next = None   # 设为错误串则下次发送失败

    async def send_code(self, provider, identifier, code):
        """返回 (外部 id, 错误信息)。code 为 None 表示由供应商托管生成。"""
        self.calls += 1
        if self.fail_next:
            err, self.fail_next = self.fail_next, None
            return None, err
        if code is None:                    # 手机：供应商托管
            code = generate_code()
            self.hosted[identifier] = {
                "code": code, "expires": time.time() + CODE_TTL,
                "attempts": 0}
        self.sent.append((identifier, code))
        return f"mock-{self.calls}", None

    async def check_hosted(self, identifier, code):
        """模拟 PNVS 的 CheckSmsVerifyCode。

        真实实现的成功条件是两层同时满足：body.code == 'OK' 且
        body.model.verifyResult == 'PASS'。只判外层会把"码错"当成"校验成功"。
        """
        rec = self.hosted.get(identifier)
        if not rec or rec["expires"] < time.time():
            self.hosted.pop(identifier, None)
            return False, "code_expired"
        rec["attempts"] += 1
        if rec["attempts"] > MAX_ATTEMPTS:
            self.hosted.pop(identifier, None)
            return False, "code_expired"
        if hmac.compare_digest(rec["code"], str(code).strip()):
            self.hosted.pop(identifier, None)     # 命中即删
            return True, None
        return False, "invalid_code"

    def peek_code(self, identifier):
        """仅测试用：取出最近发给该 identifier 的验证码。"""
        if identifier in self.hosted:
            return self.hosted[identifier]["code"]
        for ident, code in reversed(self.sent):
            if ident == identifier:
                return code
        return None
