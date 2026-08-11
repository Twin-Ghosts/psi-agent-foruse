# -*- coding: utf-8 -*-
"""自检：移植后的服务端是否仍与契约逐条一致。

移植引入了两处**副本**，副本会漂移，而漂移不会报错——只会让客户端和服务端
对不上，且在真实用户身上才暴露。所以这里逐条比对：

    1. contract_errors.ERRORS      对 contract/auth_contract.ERRORS
    2. FastAPI 实际注册的路由表     对 contract/auth_contract.ENDPOINTS

第 2 条是关键：路由前缀、方法、路径任何一处写错，客户端都会 404，而
契约测试若恰好也照错的路径打就发现不了。这里直接读 app 的路由表，
不经过 HTTP。

用法：
    python 自检_契约一致.py [--negative]
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_REPO = os.path.abspath(os.path.join(HERE, "..", "psi-agent-auth"))
sys.path.insert(0, os.path.join(HERE, "src"))
sys.path.insert(0, AUTH_REPO)

os.environ.setdefault("AUTH_DB_PATH", ":memory:")

from contract import auth_contract as C  # noqa: E402

from psi_cloud.auth import app as app_mod  # noqa: E402
from psi_cloud.auth.contract_errors import ERRORS  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global _passed
    if ok:
        _passed += 1
        print(f"  OK   {label}")
    else:
        _failed.append(label)
        print(f"  FAIL {label}" + (f" —— {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n{name}")


def registered_routes() -> set[tuple[str, str]]:
    """FastAPI 实际注册的 (方法, 路径)。只取契约前缀下的，忽略 /docs 等。"""
    out: set[tuple[str, str]] = set()
    for path, ops in app_mod.create_app().openapi()["paths"].items():
        if not path.startswith(C.PREFIX):
            continue
        for method in ops:
            out.add((method.upper(), path))
    return out


def run() -> None:
    section("[1] 错误码表与契约一致")
    check("错误码集合完全一致", set(ERRORS) == set(C.ERRORS),
          f"仅服务端={set(ERRORS) - set(C.ERRORS)} "
          f"仅契约={set(C.ERRORS) - set(ERRORS)}")
    for code, (want, _msg) in sorted(C.ERRORS.items()):
        got = ERRORS.get(code, (None, ""))[0]
        check(f"{code} → {want}", got == want, f"服务端给 {got}")

    section("[2] 路由表与契约一致")
    routes = registered_routes()
    for key, spec in sorted(C.ENDPOINTS.items()):
        # 契约的 {id} 与实现的 {device_id} 是同一个位置参数，比对时归一化：
        # 参数名属实现细节，路径结构才是契约。
        want_path = C.PREFIX + spec["path"]
        norm = {(m, _strip_param_names(p)) for m, p in routes}
        check(f"{key}: {spec['method']} {want_path}",
              (spec["method"], _strip_param_names(want_path)) in norm,
              f"实际路由表={sorted(norm)}")

    section("[3] 没有契约外的多余端点")
    want = {(s["method"], _strip_param_names(C.PREFIX + s["path"]))
            for s in C.ENDPOINTS.values()}
    # healthz 不在契约的 9 个端点里，但 Caddy/compose 探它，属既定例外
    want.add(("GET", C.PREFIX + "/healthz"))
    extra = {(m, _strip_param_names(p)) for m, p in routes} - want
    check("无契约外端点暴露在前缀下", not extra, f"多余={sorted(extra)}")

    section("[4] 测试钩子默认不挂载")
    paths = {p for p in app_mod.create_app().openapi()["paths"]}
    check("默认配置下 schema 里无 __test__ 路径",
          not any("__test__" in p for p in paths))
    # 更硬的断言：直接查路由对象，schema 之外也不能有
    all_paths = _all_route_paths(app_mod.create_app())
    check("默认配置下路由表里无 __test__ 路径",
          not any("__test__" in p for p in all_paths),
          f"命中={[p for p in all_paths if '__test__' in p]}")


def _strip_param_names(path: str) -> str:
    """把 /{id} 与 /{device_id} 归一成 /{}，只比路径结构。"""
    out: list[str] = []
    depth = 0
    for ch in path:
        if ch == "{":
            depth += 1
            if depth == 1:
                out.append("{}")
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _all_route_paths(app: object) -> list[str]:
    """遍历所有路由（含 include_in_schema=False 的）。"""
    found: list[str] = []
    stack = [app]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        path = getattr(node, "path", None)
        if isinstance(path, str):
            found.append(path)
        for attr in ("routes", "router", "app"):
            child = getattr(node, attr, None)
            if isinstance(child, list):
                stack.extend(child)
            elif child is not None:
                stack.append(child)
    return found


def main() -> int:
    if "--negative" in sys.argv:
        return negative()
    run()
    print(f"\n通过 {_passed} / {_passed + len(_failed)}，失败 {len(_failed)}")
    if _failed:
        for f in _failed:
            print(f"  - {f}")
        return 1
    print("错误码表、路由表均与契约一致；测试钩子默认不挂载。")
    return 0


def negative() -> int:
    """反向验证：植入必然改变行为的破坏点，每个都必须被抓到。

    破坏点必须是**必然**生效的 —— 概率性破坏点（比如只改一个少见分支）
    会让反向验证看起来通过、实则失效。
    """
    import copy

    global _passed, _failed
    cases = [
        ("错误码状态码改错（rate_limited 429→400）",
         lambda: ERRORS.__setitem__("rate_limited", (400, "x"))),
        ("删掉一个错误码（provider_error）",
         lambda: ERRORS.pop("provider_error", None)),
        ("加一个契约里没有的错误码",
         lambda: ERRORS.__setitem__("made_up_code", (418, "x"))),
        ("改掉契约前缀（路由全部对不上）",
         lambda: setattr(app_mod, "PREFIX", "/wrong/prefix")),
    ]

    ok_all = True
    for label, break_it in cases:
        saved_errors = copy.deepcopy(dict(ERRORS))
        saved_prefix = app_mod.PREFIX
        _passed, _failed = 0, []
        break_it()
        try:
            run()
        except Exception as exc:                      # noqa: BLE001
            _failed.append(f"抛异常：{exc!r}")
        caught = bool(_failed)
        print(f"\n  破坏点「{label}」→ {'被抓到' if caught else '未被抓到（危险）'}")
        ok_all = ok_all and caught
        ERRORS.clear()
        ERRORS.update(saved_errors)
        app_mod.PREFIX = saved_prefix

    _passed, _failed = 0, []
    run()
    print(f"\n  恢复后：失败 {len(_failed)} 项（应为 0）")
    ok_all = ok_all and not _failed

    print("\n  结论：" + ("每个破坏点都被抓到，且恢复后全绿——自检有约束力"
                          if ok_all else "有破坏点未被抓到，自检无效"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
