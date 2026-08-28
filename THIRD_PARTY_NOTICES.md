# 第三方组件与许可 · Third-Party Notices

本项目自身的代码以 **MIT** 许可发布（见 [LICENSE](LICENSE)）。
下面这些东西**不是本项目的代码**，各自沿用自己的许可。

This project's own code is released under the **MIT** licence (see [LICENSE](LICENSE)).
The components below are **not** part of this project's code and keep their own licences.

## FFmpeg / FFprobe（随应用分发）

`scripts/fetch_ffmpeg.py` 下载的是第三方**静态构建**，落在 `bin/`，打包时作为 Tauri
`externalBin` 附带发布：

| 平台 | 来源 |
|---|---|
| Windows | https://www.gyan.dev/ffmpeg/builds/ （`ffmpeg-release-essentials`） |
| Linux | https://johnvansickle.com/ffmpeg/ （`ffmpeg-release-amd64-static`） |
| macOS | https://evermeet.cx/ffmpeg/ |

这三家的构建都开了 `--enable-gpl`，因此**二进制本身是 GPL 的**（部分构建含
`--enable-nonfree` 组件，本项目不使用那类构建）。两件事因此成立，也是刻意如此：

- 本应用**只把 FFmpeg 当外部进程调用**（`app/core/ffmpeg.py` 是唯一入口，起子进程传参数），
  不链接它的任何库，所以两者属于聚合分发（mere aggregation），MIT 的本体代码不因此变成 GPL；
- 但**分发安装包的人要履行 GPL 的义务**：随包提供 FFmpeg 的许可全文与对应源码（或有效的源码
  获取途径）。上面每个来源的页面都给出了所用版本的源码位置与编译参数，转发时请一并带上。

不想附带 GPL 二进制时有一条现成的出路：不跑 `scripts/fetch_ffmpeg.py`，让用户自己装，
`AIVS_FFMPEG_PATH` 指过去即可（查找顺序是 **显式配置 → 内置副本 → PATH**）。

The three builds above are compiled with `--enable-gpl`, so **the binaries themselves are GPL**.
This application only ever *invokes* FFmpeg as an external process (`app/core/ffmpeg.py` is the
single entry point) and never links its libraries, so the two are merely aggregated — the MIT
licence of this project's own code is unaffected. Whoever **distributes an installer** containing
those binaries must still satisfy the GPL: ship FFmpeg's licence text and the corresponding source
(or a valid written offer for it). Alternatively, skip `scripts/fetch_ffmpeg.py` entirely and let
users install FFmpeg themselves — point `AIVS_FFMPEG_PATH` at it.

## 依赖库

后端（`backend/pyproject.toml`）与前端（`frontend/package.json`）的依赖都是各自生态里常见的
宽松许可（MIT / BSD / Apache-2.0 / PSF）；桌面壳依赖 Tauri（MIT 或 Apache-2.0 双许可）。
完整清单请用 `pip licenses` / `npm-license-checker` / `cargo about` 一类工具从锁文件生成——
手抄一份到这里迟早会和锁文件对不上。

## ComfyUI 与模型

本项目**不分发** ComfyUI、任何模型权重或工作流图（预设图属于用户自己那台机器，
连导出包里都只带「要一份标了这几个入口的图」的清单，不带图本身）。
ComfyUI、模型权重与 LoRA 的许可由各自的发布方决定，与本项目无关。

This project does **not** distribute ComfyUI, model weights, or workflow graphs. Their licences are
set by their respective publishers.

---

以上是事实陈述，不是法律意见。真要对外分发安装包时，请自行核对当次实际打进包里的每个二进制。

The above is a statement of fact, not legal advice. Verify every binary you actually ship.
