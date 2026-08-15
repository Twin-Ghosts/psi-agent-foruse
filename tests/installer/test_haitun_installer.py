"""HaiTun 安装流程全量测试。

把安装流程真实踩过的坑固化成可重复测试, 覆盖 Inno Setup 脚本
(``.github/inno-setup/haitun.iss``)与两份协议 HTML 的所有已知错误点:

静态检查(无需 ISCC, CI/任何环境都能跑):
  - .iss 必须是 UTF-8 with BOM(否则中文向导页乱码)
  - Pascal ``{ }`` 注释里不能嵌 ``{...}``(否则编译语法错误)
  - TNewCheckBox 不能用 WordWrap 属性(否则 Unknown identifier)
  - 静默安装门禁放行(WizardSilent, 否则卡死自动更新)
  - 编译期护栏(缺失/占位 haitun.exe 拒绝打包)
  - 运行期护栏(ExeLooksValid + LaunchBrokenExe 提示)
  - 「查看」按钮文件名 == [Files] dontcopy 打包名(否则查看打不开)
  - CustomMessages 的 Legal* 键中英文都齐全
  - 两份协议 HTML 完整(UTF-8、privacy 含表格、无外链)

真编译(仅在有 ISCC 时跑, 否则 skip):
  - 用有效占位 exe 能编译成功
  - 用过小占位 exe 会被编译期护栏拒绝
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INNO_DIR = REPO_ROOT / ".github" / "inno-setup"
ISS = INNO_DIR / "haitun.iss"
LEGAL_DIR = INNO_DIR / "legal"
TERMS = LEGAL_DIR / "service-agreement.html"
PRIVACY = LEGAL_DIR / "privacy-policy.html"


@pytest.fixture(scope="module")
def iss_bytes() -> bytes:
    return ISS.read_bytes()


@pytest.fixture(scope="module")
def iss_text() -> str:
    # BOM 存在时用 utf-8-sig 去掉 BOM 再读正文
    return ISS.read_text(encoding="utf-8-sig")


# ----------------------------------------------------------------- 存在性
def test_iss_exists() -> None:
    assert ISS.is_file(), f"缺少安装脚本: {ISS}"


def test_legal_files_exist() -> None:
    assert TERMS.is_file(), "缺少《软件许可及服务协议》HTML"
    assert PRIVACY.is_file(), "缺少《隐私保护政策》HTML"


# ----------------------------------------------------------------- 编码
def test_iss_has_utf8_bom(iss_bytes: bytes) -> None:
    # 含中文的 .iss 必须带 UTF-8 BOM, 否则 Inno Setup 按系统 ANSI(GBK)
    # 误解码 → 向导页中文乱码。
    assert iss_bytes[:3] == b"\xef\xbb\xbf", "haitun.iss 缺 UTF-8 BOM, 中文会乱码"


def test_iss_valid_utf8(iss_bytes: bytes) -> None:
    iss_bytes.decode("utf-8")  # 不抛异常即合法


# ----------------------------------------------------------------- 语法坑
def test_no_nested_brace_in_brace_comments(iss_text: str) -> None:
    # Pascal ``{ ... }`` 注释里再出现 ``{`` 会触发语法错误。
    # 只检查以 ``{`` 起始的整行注释是否内部还含 ``{``。
    for i, line in enumerate(iss_text.splitlines(), 1):
        s = line.lstrip()
        if s.startswith("{") and not s.startswith("{#") and not s.startswith("{cm:"):
            inner = s[1:]
            assert "{" not in inner, f"第{i}行 {{ }} 注释里嵌了 {{, 会编译语法错误: {line!r}"


def test_checkbox_no_wordwrap(iss_text: str) -> None:
    # TNewCheckBox 没有 WordWrap 属性(只有 TNewStaticText 有)。
    assert not re.search(r"LegalAgreeCheck\.WordWrap", iss_text), (
        "TNewCheckBox 不支持 WordWrap, 会报 Unknown identifier"
    )


# ------------------------------------------------------------- 同意页/门禁
def test_consent_page_present(iss_text: str) -> None:
    assert "CreateCustomPage(wpWelcome" in iss_text, "缺少欢迎页后的自定义同意页"
    assert "LegalAgreeCheck" in iss_text, "缺少同意勾选框"
    assert "NextButtonClick" in iss_text, "缺少门禁 NextButtonClick"


def test_silent_install_bypasses_gate(iss_text: str) -> None:
    # 静默安装(自动更新)必须放行, 否则同意门禁把静默更新卡死。
    m = re.search(r"function NextButtonClick.*?end;", iss_text, re.S)
    assert m, "找不到 NextButtonClick 函数体"
    assert "WizardSilent()" in m.group(0), "NextButtonClick 缺 WizardSilent 放行, 静默更新会卡死"


# --------------------------------------------------------------- 双护栏
def test_compile_time_guard(iss_text: str) -> None:
    # 编译期护栏: 缺失/过小的 haitun.exe 拒绝打包。
    assert 'FileExists("haitun.exe")' in iss_text, "缺编译期护栏:未检查 haitun.exe 是否存在"
    assert re.search(r'FileSize\("haitun\.exe"\)\s*<\s*\d+', iss_text), "缺编译期护栏:未检查 haitun.exe 体积(防占位/桩)"


def test_runtime_guard(iss_text: str) -> None:
    # 运行期护栏: 装完校验主程序有效, 否则给清晰中文提示(而非系统 216)。
    assert "ExeLooksValid" in iss_text, "缺运行期护栏 ExeLooksValid"
    assert "LaunchBrokenExe" in iss_text, "缺主程序损坏时的用户提示消息"


# ----------------------------------------------------- 查看按钮 vs dontcopy
def test_view_buttons_match_dontcopy_files(iss_text: str) -> None:
    # 「查看」按钮 OpenLegalDoc('x.html') 的文件名, 必须与 [Files] 里
    # dontcopy 打包的 basename 一致, 否则 ExtractTemporaryFile 找不到文件。
    opened = set(re.findall(r"OpenLegalDoc\('([^']+)'\)", iss_text))
    dontcopy = set(re.findall(r'Source:\s*"legal\\([^"]+)";\s*Flags:\s*dontcopy', iss_text))
    assert opened, "没找到任何 OpenLegalDoc 调用"
    assert dontcopy, "没找到任何 dontcopy 的 legal 文件"
    missing = opened - dontcopy
    assert not missing, f"这些「查看」文件名没被 dontcopy 打包, 会打不开: {missing}"


def test_dontcopy_files_exist_on_disk(iss_text: str) -> None:
    for name in re.findall(r'Source:\s*"legal\\([^"]+)";\s*Flags:\s*dontcopy', iss_text):
        assert (LEGAL_DIR / name).is_file(), f"dontcopy 引用的文件不存在: legal/{name}"


# ------------------------------------------------- CustomMessages 中英齐全
def test_custom_messages_bilingual(iss_text: str) -> None:
    # 所有 {cm:LegalXxx} 引用, 必须在 chinesesimplified. 和 english. 下都有定义。
    referenced = set(re.findall(r"\{cm:(Legal\w+)\}", iss_text))
    assert referenced, "没找到任何 {cm:Legal*} 引用"
    for lang in ("chinesesimplified", "english"):
        defined = set(re.findall(rf"^{lang}\.(\w+)=", iss_text, re.M))
        missing = referenced - defined
        assert not missing, f"{lang} 缺这些消息定义: {missing}"


# --------------------------------------------------------- 协议 HTML 完整性
@pytest.mark.parametrize("path", [TERMS, PRIVACY])
def test_legal_html_is_utf8_html(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "<html" in text.lower(), f"{path.name} 不是合法 HTML"
    assert len(text) > 1000, f"{path.name} 内容过短, 疑似不完整"


def test_privacy_has_tables() -> None:
    # 隐私政策含权限/数据表格, 转换时必须保留。
    text = PRIVACY.read_text(encoding="utf-8")
    assert text.lower().count("<table") >= 1, "隐私政策的表格丢失了"


@pytest.mark.parametrize("path", [TERMS, PRIVACY])
def test_legal_html_self_contained(path: Path) -> None:
    # 协议页随安装器离线打开, 不能依赖会发起网络请求的外部资源。
    # 注意: ``xmlns="http://www.w3.org/..."`` 是 XML 命名空间声明(标准、不联网),
    # 不算外链, 故只检测真正会加载资源的 src/href。
    text = path.read_text(encoding="utf-8").lower()
    assert "<img" not in text, f"{path.name} 含外部图片, 离线打不全"
    assert "<script" not in text, f"{path.name} 含脚本, 安装器内不该有"
    assert not re.search(r'src\s*=\s*["\']https?://', text), f"{path.name} 含联网加载的资源(src), 应自包含"
    assert not re.search(r'href\s*=\s*["\']https?://', text), f"{path.name} 含外链样式/资源(href), 应自包含"


# --------------------------------------------------------------- 真编译
def _find_iscc() -> str | None:
    for name in ("ISCC.exe", "iscc"):
        p = shutil.which(name)
        if p:
            return p
    for c in (
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ):
        if Path(c).is_file():
            return c
    return None


ISCC_PATH = _find_iscc()


@pytest.mark.skipif(ISCC_PATH is None, reason="未安装 Inno Setup ISCC, 跳过真编译")
def test_real_compile_rejects_placeholder(tmp_path: Path) -> None:
    # 编译期护栏: 过小的 haitun.exe(占位/桩)必须被拒绝, 编译失败。
    assert ISCC_PATH is not None  # skipif 已保证; 供类型收窄
    (INNO_DIR / "haitun.exe").write_bytes(b"MZ")  # 2 字节占位
    try:
        r = subprocess.run(
            [ISCC_PATH, f"/O{tmp_path}", str(ISS)],
            capture_output=True,
            text=True,
            cwd=str(INNO_DIR),
            timeout=120,
        )
        assert r.returncode != 0, "占位 haitun.exe 竟然编译通过了(护栏失效)"
    finally:
        (INNO_DIR / "haitun.exe").unlink(missing_ok=True)


@pytest.mark.skipif(ISCC_PATH is None, reason="未安装 Inno Setup ISCC, 跳过真编译")
def test_real_compile_succeeds_with_valid_exe(tmp_path: Path) -> None:
    # 用一个真实的、足够大的 PE(拿系统 exe 冒充)当 haitun.exe, 应能编译成功。
    assert ISCC_PATH is not None  # skipif 已保证; 供类型收窄
    sysexe = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "hostname.exe"
    if not sysexe.is_file() or sysexe.stat().st_size < 4096:
        pytest.skip("找不到合适的占位 PE 用于真编译")
    (INNO_DIR / "haitun.exe").write_bytes(sysexe.read_bytes())
    isl = INNO_DIR / "ChineseSimplified.isl"
    isl_created = False
    try:
        # ChineseSimplified.isl 是 CI 运行时下载的; 本地缺失则跳过(需要它才能编)。
        if not isl.is_file():
            pytest.skip("缺 ChineseSimplified.isl(CI 运行时下载), 跳过成功编译用例")
        r = subprocess.run(
            [ISCC_PATH, f"/O{tmp_path}", str(ISS)],
            capture_output=True,
            text=True,
            cwd=str(INNO_DIR),
            timeout=180,
        )
        assert r.returncode == 0, f"有效 exe 下编译失败: {r.stdout[-800:]}{r.stderr[-800:]}"
        assert list(tmp_path.glob("*.exe")), "编译成功但没产出安装包 exe"
    finally:
        (INNO_DIR / "haitun.exe").unlink(missing_ok=True)
        if isl_created:
            isl.unlink(missing_ok=True)
