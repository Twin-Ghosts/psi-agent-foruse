# 已知问题：spa-v2 用了 vitest 但没声明依赖

**这是既有问题，不是本次改动引入的。** 单独记录，供评审决定是否另开 issue。
本次 diff 刻意**不修**它——修 `package.json` 属于另一件事，会污染登录功能的改动范围。

## 现象

`src/services/` 下有 12 个 `*.test.ts` 用 `import { describe, expect, it } from 'vitest'`，
但 `package.json` 里：

- `devDependencies` 没有 `vitest`
- `scripts` 没有 `test`

后果有两条，第二条更隐蔽：

1. **测试跑不起来。** 全新 clone 后 `npx vitest` 会失败（没装）。
2. **这些文件让 `tsc --noEmit` 报错。** 因为 `vitest` 模块无类型可解析。实测
   全库 17 个类型错误里，有 8 个来自这些 `.test.ts`。也就是说**类型检查现在本来就
   是红的**，新错误容易被淹没在既有噪音里。

## 复现

```bash
cd src/psi_agent/gateway/spa-v2
npm ci
npx vitest run          # 失败：找不到 vitest
npx tsc --noEmit        # 报错，含 8 个来自 *.test.ts 的 "Cannot find module 'vitest'"
```

## 建议修法

```jsonc
{
  "scripts": {
    "test": "vitest run",
    "typecheck": "tsc --noEmit"
  },
  "devDependencies": {
    "vitest": "^4.1.10"      // 与本地实测通过的版本一致
  }
}
```

另有两个 `tsconfig.json` 的 TypeScript 7 兼容问题（`baseUrl` 已被移除、
`paths` 需相对路径），同属既有问题，一并修更省事：

```
tsconfig.json(18,5): error TS5102: Option 'baseUrl' has been removed.
tsconfig.json(20,15): error TS5090: Non-relative paths are not allowed.
```

## 本次改动如何绕过

`自检_SPA登录.py` 用两个办法在不改仓库文件的前提下完成验证：

1. 生成临时 `tsconfig.selfcheck.json`（去掉 `baseUrl`/`paths`），跑完即删；
2. 类型检查**只统计本次改动的文件**，不要求全库零错误——否则 8 个既有错误会让
   这条断言永远红，久而久之就被忽略。

vitest 用 `npm install --no-save vitest` 装，不写进 `package.json`。

这些绕法是权衡后的选择，不是最终状态：等 `package.json` 补齐后，
`自检_SPA登录.py` 里的临时 tsconfig 与文件过滤都可以去掉。
