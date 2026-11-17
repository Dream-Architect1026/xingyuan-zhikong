# 第三方组件与开源许可

版本：2.1.1

本程序以动态链接、未修改的方式使用下列第三方组件。组件清单按实际随包文件核对；许可证标识取自各组件包元数据。安装目录与便携包内的 `LICENSES/` 收录了逐组件的许可证原文或声明文件。

## 1. 组件清单

| 组件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| PySide6 / shiboken6（含 Qt 6） | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only（本项目按 LGPL-3.0 使用） | 桌面界面 |
| pyqtgraph | 0.14.0 | MIT | 曲线绘制 |
| numpy | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 数值计算 |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT | 本机 HTTP 服务 |
| bleak | 3.1.0 | MIT | 蓝牙低功耗 |
| paho-mqtt | 2.2.0 | EPL-2.0 OR BSD-3-Clause（本项目按 BSD-3-Clause 使用） | MQTT 客户端（默认关闭） |
| winrt-*（15 个子包） | 3.2.1 | MIT | 蓝牙与系统通知接口 |
| requests | 2.34.2 | Apache-2.0 | 云接口调用 |
| PyYAML | 6.0.3 | MIT | 配置解析 |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | 加密算法 |
| pycryptodomex（Cryptodome） | 3.24.2 | BSD, Public Domain | 设备协议加解密 |
| pycryptodome（Crypto） | 3.23.0 | BSD, Public Domain | 加密依赖（兼容副本） |
| psutil | 7.2.2 | BSD-3-Clause | 进程与资源信息 |
| pywin32 | 312 | PSF | Windows COM 集成 |
| Pillow | 12.3.0 | MIT-CMU | 图像处理 |
| attrs | 26.1.0 | MIT | aiohttp 依赖 |
| yarl / multidict / frozenlist / propcache | 1.25.1 / 7.2.1 / 1.8.0 / 0.5.4 | Apache-2.0 | aiohttp 依赖 |
| charset-normalizer | 3.5.1 | MIT | requests 依赖 |
| certifi | 2026.7.22 | MPL-2.0 | CA 证书包 |
| setuptools | 78.1.0 | MIT | 运行时组件 |

## 2. 履行方式

1. **LGPL-3.0（PySide6 / Qt / shiboken6）**：以动态链接、未修改方式使用；相关库以独立 DLL 形式位于程序目录，用户可自行替换为兼容版本。本项目不要求用户接受额外限制。
2. **MIT / BSD / Apache-2.0 / PSF / MPL-2.0**：保留版权声明与许可证文本，不附加额外限制。
3. **pycryptodomex**：保留其 BSD 许可证与公有领域声明。
4. 本项目未修改上述任何组件源码；如后续修改，将在本文件登记内容与日期。
5. 本项目自身代码按 MIT 授权（`Copyright (c) 2026 X.H`），全文见 [LICENSE](LICENSE)。
6. **paho-mqtt**：以 EPL-2.0 OR BSD-3-Clause 双许可发布，本项目按 BSD-3-Clause 使用；MQTT 集成默认关闭，仅在用户主动配置并开启后运行。

## 3. 核对结果与已知缺口

1. `winrt-*` 共 15 个子包，版本均为 3.2.1，元数据均为 `License-Expression: MIT`；这些子包的 `dist-info` 内未附许可证原文文件，随包以声明文件形式收录。
2. 安装目录的第三方 `dist-info` 中，仅 `pycryptodomex` 附带许可证原文；其余只声明许可证标识，随包以声明文件形式收录。
3. PySide6 与 shiboken6 未随包提供 LGPL-3.0 原文，因此由本项目在 `LICENSES/` 内收录全文。
4. PySide6 附带的 Qt 子模块（如 ICU、SQLite、zlib 等）可能含额外第三方许可，按随包实际文件逐项登记。

## 4. 参考实现与引用边界

本节的参考项不计入随包组件，仅说明思路、协议事实与文档指引的来源。

| 参考项 | 许可证 | 本项目用到的部分 | 改动 |
|---|---|---|---|
| Dream-Architect1026/cuktech-ble-server | MIT | MiOT BLE 通道与认证/命令帧、会话与采样点表结构、巴法云配置与 topic 用法 | 改为桌面端；新增分箱、学习模型、自动化与本地 AI；MQTT 默认关闭 |
| zhyzhaogit/cuktech-ble-controller | MIT | GATT 服务/特征与命令帧字段定义（对照） | 按本项目结构重写，未复制代码 |
| zuyan9/ha-cuk-ble | Apache-2.0 | 协议检测顺序与档位依据 | 仅保留展示字段，未引入其集成代码 |
| kairui1108/cuktech-ble-server | 未声明 | 参考项目的上游，仅参考思路与协议事实 | 未复制代码 |
| PiotrMachowski/Xiaomi-cloud-tokens-extractor | MIT | 安装文档中的 Token 获取指引 | 工具不随包分发 |

- 被引用部分遵循原项目许可证；本项目的 MIT 不覆盖被引用部分。
- 未修改上述任何项目的代码后再分发；随包只包含本项目代码与第 1 节所列第三方组件。

## 5. 许可证文本的获取

- 安装目录与便携包内均提供 `LICENSES/`：逐组件收录许可证原文；包内未附原文的组件收录声明文件（包名、版本、许可证标识、包主页、核查日期）。
- 构建流程对 `LICENSES/` 做完整性断言：文件数少于实际打包的组件数时构建失败。
- 历史版本（1.3.4 及更早）的许可原文，可通过 X.H 团队的发布渠道索取。
