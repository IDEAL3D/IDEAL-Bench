# IDEAL Metrics Definition (Draft)

本文档用于统一 IDEAL 的评测定义、公式与实现约束，覆盖硬指标与软指标。

## 1. 编号与函数一一对应

以下编号与 `benchmarks/evaluation/[WIP]metrics.py` 中函数一一对应：

| 编号 | 指标 | 函数 |
|---|---|---|
| 2.2.1 | 位置错误率（PE） | `metric_position_error_rate` |
| 2.2.2 | BBOX-IoU | `metric_bbox_iou` |
| 2.2.3 | 旋转错误率（RE） | `metric_rotation_error_rate` |
| 2.2.4 | 物体碰撞率（OOR） | `metric_object_collision_rate` |
| 2.2.5 | 物体出界率（OOB） | `metric_out_of_bounds_rate` |
| 2.3.1 | 物体识别率（RR） | `metric_recognition_rate` |
| 2.3.2 | 幻觉比率（HR） | `metric_hallucination_rate` |
| 2.4.1 | 房间类型准确率 | `metric_room_type_accuracy` |
| 2.5.1 | JSON 失败率 | `metric_json_failure_rate` |

总入口：`evaluate_scene`（汇总上述全部指标）。

## 2. 硬指标（Hard Metrics）

### 2.1 全局符号定义

对于单个场景（单张图）定义：

- $N$：GT 中应识别物体总数
- $K$：模型成功识别并正确匹配到 GT 的物体数，满足 $0 \le K \le N$
- $M$：模型识别出的“场景中不存在物体”（幻觉物体）数量
- $Q$：预测场景中与其他预测物体发生不可接受重叠的物体数量
- $P$：预测场景中与墙面重叠或在房间包络线之外的物体数量

注：$Q$、$P$ 属于“与 GT 无关”的几何合法性指标。

---

### 2.2 维度一：几何与空间准确性（Geometry & Spatial Accuracy）

#### A. 与 GT 有关

#### 2.2.1 位置错误率（Position Error, PE）

- 定义：GT 物体几何中心与预测物体几何中心在世界坐标系下的欧氏距离。
- 默认建议：仅比较 XY 平面距离，避免资产 pivot 与地面挤出造成的 Z 偏差干扰。

若第 $i$ 个匹配对误差为 $d_i$：

$$
d_i^{xy} = \sqrt{(x_i^{pred}-x_i^{gt})^2 + (y_i^{pred}-y_i^{gt})^2}
$$

$$
PE = \frac{1}{K}\sum_{i=1}^{K} d_i^{xy}
$$

可选 3D 版本：

$$
d_i^{xyz} = \sqrt{(x_i^{pred}-x_i^{gt})^2 + (y_i^{pred}-y_i^{gt})^2 + (z_i^{pred}-z_i^{gt})^2}
$$

#### 2.2.2 BBOX-IoU（匹配对）

- 定义：仅在成功匹配到 GT 的对象对上计算 3D BBox IoU。
- 评估关注：
  - 平均 IoU（匹配样本上）
  - 通过率（IoU 大于阈值，建议阈值区间 0.1-0.3）

对于第 $i$ 个匹配对：

$$
IoU_i = \frac{V(B_i^{gt} \cap B_i^{pred})}{V(B_i^{gt} \cup B_i^{pred})}
$$

$$
\text{Mean-IoU} = \frac{1}{K}\sum_{i=1}^{K} IoU_i
$$

通过率可按两种口径报告：

$$
\text{IoU-Pass@}\tau\text{(over N)} = \frac{\#\{i: IoU_i \ge \tau\}}{N}
$$

$$
\text{IoU-Pass@}\tau\text{(over K)} = \frac{\#\{i: IoU_i \ge \tau\}}{K}
$$

#### 2.2.3 旋转错误率（Rotation Error, RE）

- 仅在“旋转敏感类别”上计算（例如 bed、sofa）。
- 对称物体默认豁免或采用等价角规则（详见 2.3.5）。

对于有效匹配集合 $\Omega_{rot}$：

$$
\Delta\theta_i = \min\left(|\theta_i^{pred}-\theta_i^{gt}|,\ 360-|\theta_i^{pred}-\theta_i^{gt}|\right)
$$

$$
RE = \frac{1}{|\Omega_{rot}|}\sum_{i \in \Omega_{rot}} \Delta\theta_i
$$

可报告归一化版本：$RE_{norm}=RE/180$。

#### B. 与 GT 无关

#### 2.2.4 物体碰撞率（Object Overlap Rate, OOR）

- 定义：预测对象中发生不可接受几何重叠的物体比例。
- 若重叠物体数量为 $Q$：

$$
OOR = \frac{Q}{N}
$$

#### 2.2.5 物体出界率（Out-of-Bounds Rate, OOB）

- 定义：预测对象中与房间边界相交/越界的物体比例。
- 若出界物体数量为 $P$：

$$
OOB = \frac{P}{N}
$$

说明：以上 OOB/OOR 公式与 IL3D 风格定义一致，强调几何合法性约束。

---

### 2.3 维度二：物体识别能力（Object Recognition）

用于观察模型在类别识别上的召回与幻觉特性。

#### 2.3.1 物体识别率（Recognition Rate, RR）

$$
RR = \frac{K}{N}
$$

#### 2.3.2 幻觉比率（Hallucination Rate, HR）

$$
HR = \frac{M}{K}
$$

注：当 $K=0$ 时，需定义退化处理（例如记为 1 或记为 NaN 并单独统计）。建议论文中明确。

---

### 2.4 维度三：场景识别能力（Scene Type Recognition）

#### 2.4.1 房间类型准确率

针对房间类型分类能力，按场景集合统计。

- 设某户型/场景集合共推理 $N_s$ 次，其中识别正确 $K_s$ 次：

$$
\text{Room-Type Accuracy} = \frac{K_s}{N_s}
$$

---

### 2.5 维度四：结构化输出能力（Structured Output Robustness）

#### 2.5.1 JSON 失败率

关注模型输出是否可被机器消费。

- 设总输出 JSON 数量为 $T$，无法解析或无法通过 schema 校验的数量为 $F$：

$$
\text{JSON Failure Rate} = \frac{F}{T}
$$

建议将失败细分为：
- 语法失败（无法 parse）
- schema 失败（字段缺失/类型错误/值域错误）

---

### 2.6 特别注意（豁免与等价规则）

#### A. Yaw 豁免原则

1) 完全对称或朝向无关类别（如 plant/lamp/rug）
- `rotation_yaw` 可忽略，不纳入 RE。

2) 中心对称类别等价角（示例：simpledesk/blackboard/library blocks/cellfactory/coffee table）
- 当 GT=270 时，预测 270 或 90 视为正确
- 当 GT=180 时，预测 180 或 0 视为正确

3) SideTable 为方形时
- GT 为 0/90/180/270 时，预测在该集合内任意值都可判定为正确

4) coffee table 朝向特殊性
- 资产前向定义历史问题已在 GT 导出层修复；评测仍应保留白名单以兼容旧样本。

#### B. Position.z 豁免原则

- Infinigen 中部分资产根原点的 `position.z` 受资产建模与地面结构影响，存在系统偏移与跨资产不一致。
- 默认不比较 `position.z`（仅比较 XY），可显著提高几何指标稳定性。
- 例外：墙面相关物体（如 wall art / mirror）可保留 z 比对，因为其高度是空间语义的一部分。

---

## 3. 软指标（Soft Metrics）

软指标用于补充 object-level hard metrics 无法表达的“场景级空间理解质量”。

### 3.1 场景重建协议

将预测布局 $S_{pred}$ 回写到原始 `scene.blend`：

- 对每个匹配对象，用预测的 `position / yaw / dimensions` 替换 GT
- 资产几何、相机参数、光照保持不变
- 渲染得到重建图 $I_{pred}$

因此，$I$ 与 $I_{pred}$ 的差异主要来自布局理解误差，而不是材质或资产风格差异。

协议细节：
- 落地物体：通常固定垂直落点（可采用 GT 高度）以避免浮空/穿地伪影
- 墙面物体（如 mirror、wall art）：保留预测高度以体现空间推理能力

该协议完整说明见附录（Appendix B）。

### 3.2 Judge VLM

使用冻结的多模态模型 $J$ 作为自动评判器：

- 单次请求同时输入：参考图 + 同场景多模型重建图
- 目标：降低跨请求评分漂移（inter-call variance）并节约 API 成本
- 输出：结构化分数（按维度打分）

提示词见 Appendix B.3。

### 3.3 评分维度（Likert 1-5）

#### 维度一：整体空间印象相似度（Overall Spatial Impression Similarity）

引导问题：
- 一眼看去是否是“同一种房间”？
- 主区域、用途感、空间划分是否一致？

评分锚点：
- 1：完全不像（房间类型/用途错位明显）
- 2：明显偏差（方向对但主区域错得多）
- 3：大致一致（可识别同类空间，但重心偏差明显）
- 4：较一致（主区域基本正确，仅细节偏差）
- 5：高度一致（一眼同一空间印象）

#### 维度二：空间层次与深度感（Spatial Layering and Depth）

引导问题：
- 前景/中景/背景是否自然？
- 透视与遮挡是否合理？
- 是否存在“纸片感”、漂浮或贴墙违和？

评分锚点：
- 1：层次混乱或极平
- 2：层次弱，前后关系不自然
- 3：一般，有基本深度感但局部违和
- 4：较好，前后关系清晰
- 5：自然，深度感接近参考图

### 3.4 软指标风险与报告建议

- 主要风险：Judge VLM 评分稳定性（inter-call variance）
- 建议在论文中报告：
  - 同一批样本重复评分方差
  - 评分维度间相关性
  - 不同 judge 模型的一致性对比（可选）

---

## 4. 报告模板建议（每个模型）

### Hard Metrics

- Geometry: `PE_xy`, `Mean-IoU`, `IoU-Pass@0.1/0.3`, `RE`（旋转敏感类）
- Legality: `OOR(Q/N)`, `OOB(P/N)`
- Recognition: `RR(K/N)`, `HR(M/K)`
- Room Type: `Accuracy(K_s/N_s)`
- Structured Output: `JSON Failure Rate(F/T)`

### Soft Metrics

- `Overall Spatial Impression` (1-5)
- `Spatial Layering & Depth` (1-5)
- Judge 稳定性统计（方差/重跑一致性）

---

## 5. 与实现的对应关系（建议）

- `benchmarks/evaluation/[WIP]metrics.py`：实现 2. 全部硬指标（按 2.x.y 编号）
- `reconstruction/`：执行 3.1 的重建与渲染
- `benchmarks/evaluation/` 后续新增 judge 脚本：执行 3.2 与 3.3
