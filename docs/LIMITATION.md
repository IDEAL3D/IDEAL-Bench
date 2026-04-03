# 本批数据集（IDL3D `generation/` 管线）的局限与约定

<!--
  【协作者 / Agent】本文件由用户维护，用于记录管线中**遗憾未做 / 暂不完善**的约定，不是自动生成的规格书。
  请勿在未获用户要求时编辑；预处理与 JSON 格式说明以 PREPROCESS_DESIGN / geometric_rules_floor 等为准。
-->

用于 Benchmark 与论文方法章节披露；与 `GT.json`、`info.json` 的字段语义以 `PREPROCESS_DESIGN.md` 及 `geometric_rules_floor.md` 为准（本文件不逐字段镜像 JSON）。

## 房间几何：仅使用 box 矩形单房

- **默认且唯一推荐的生成入口**：`generate.py` → `generate_box.py`。房间地面 footprint 为轴对齐矩形，**在 XY 上以 Blender 世界原点为中心**，边长为 \(w \times d\)（米），与 `living_space.floor_plan` / `working_space` 中 `shapely.box(-w/2,-d/2,w/2,d/2)` 一致。
- **不规则房间（irregular）已弃用**：旧脚本路径曾为 Infinigen `MYSCRIPTS/DEPRECATED/generate_irregular.py`（未迁入本仓库 `generation/`）。该管线**不可控**：易出现不合常理的平面形状、网格破损、大量无法布置家具的碎区；本数据集**不再扩展** irregular 场景，论文中请只报告 box 子集。


## 相机与成像

- **针孔模型、无镜头畸变**：渲染为理想投影；全数据一致，不在 `info.json` 中单列畸变字段。不包含真实镜头/去畸变流程。
- **单相机**：每个场景目录对应**一张**主渲染图与**同一**相机下的 `info.json`；不提供多机位、多时刻的标定 bundle。
- **内外参**：由 Blender / Infinigen 场景与 `infinigen.core.util.camera` 导出；世界系为 Blender 世界坐标，与 OpenCV 相机系通过 `world_to_camera_rt` 约定。


## 场景与光照

- **合成室内场景（Infinigen）**：布局、材质、光照由生成管线决定。
- **光照未做「多风格」覆盖**：本批配置下**不**追求室内外真实多样光照；强度等**主要随房间尺度/面积等生成参数变化**，而非独立的光照 benchmark。若需「光照鲁棒性」评估，应另扩数据或标注光照类型。

## 几何与语义

- **矩形房间**（box 模式）。
- **类别**：`categories_in_view` 为视锥内可见的工厂类名子集，**不是**全场景物体类别全集。
- **资产**：Infinigen 程序化资产；与真实扫描或 Trellis 等外部资产在分布上不一致时，需在论文中区分「合成 / 真实」子集。



很多资产的物体原点，z = 0.14，几乎是在地板上，并且是统一的。有的资产会z更高，不统一。我觉得这个高度的问题可以在做硬算法时忽略高度不同这一点。否则的话很多资产会悬空，统计出来会有很大错误。