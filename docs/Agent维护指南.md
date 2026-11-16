# Agent维护指南

本指南随仓库发布，面向接手代码、识别和模型迭代的代理。仓库根目录的[AGENTS.md](../AGENTS.md)是工作入口；[母后使用指南](母后使用指南.md)和[迭代指南](迭代指南.md)面向使用者。

## 接手顺序

1. 读README、AGENTS、[使用说明](使用说明.md)、[架构说明](架构说明.md)和[开发说明](开发说明.md)，按本次请求确定范围。
2. 检查Git状态、Python环境、实际权重和 `models/current.json`。通过 `model_registry.current(task)` 解析部署，不根据文件名猜“最新”。
3. 本地若有编号文档：01看当前状态，00看规则，02找文件，05看操作，03看原理；06和history仅追溯时读。公开克隆没有这些本地记录时，使用本指南并检查实际文件，不能假定作者数据仍在。
4. 明确本轮输入名单、输出位置、是否联网、是否训练、验收方法。新扫描走方向校正，已转正工作图才使用 `--working-images`。

当前实现含方向、M1、M2、M3、Paddle题号、涂黑及豆包转录；完整识别在CPU FP32运行。身份区当前仍会送豆包，动画中的本地身份模型方案尚未接入生产。标准答案整理、评分与成绩导出尚未接入。

## 常用命令

以下在项目根目录执行，`python` 指已核实的项目解释器。示例中的目录和本轮名单由Agent准备，不能让使用者猜参数。

```powershell
# 新扫描：完整识别；会调用豆包，使用已有配置的ARK_API_KEY
python -B pipeline/run.py --recognize --input "D:/papers" --out "D:/grading-results/run-001" --concurrency 4
# 仅本地检测与拼图：在上条命令后增加 --detect-only
python -B tools/识别验收.py --dir "D:/grading-results/run-001" --port 8903

# M3审核：本轮CSV需包含new_name列，且对应已经准备好的M3数据
python -B tools/标注工具/serve.py --data data/m3 --subset "D:/review/m3-batch.csv" --port 8910 --no-browser

# 查询真实支持的快照、训练参数
python -B tools/迭代.py snapshot --help
python -B tools/迭代.py train --help
python -B tools/迭代.py status --run "本轮run名"
# 训练监控默认端口8901；启动前核对是否已占用
python -B tools/训练监控/serve.py --run "train/本轮run名"
```

数字审核支持 `--data`、`--port`；涂黑审核支持重复 `--data`、`--subset`、`--unreviewed`、`--port`。先查各自 `--help`，避免与识别验收端口冲突。服务API可用与真实浏览器操作通过分别记录，不能互相代替。

## 数据从哪里来

| 模块 | 输入 | 工具类别编号 |
|---|---|---|
| M1 | 转正整页 | 姓名学号0、选择题区1、填空题区4 |
| M2 | 选择题区裁图 | 题号2、选项3 |
| M3 | 填空／实验区裁图 | 完整大题5、大题号6 |

YOLO内部类别为连续编号，使用现有代码映射，不直接把工具编号当模型类别。M3大题框包含题号、分值、小题和完整作答；题号框可与大题框重叠。

检测库位于 `data/<task>/`：`images/` 存工作图，`log/` 存逐图人工事件，`manifest.csv` 记录来源，`splits.csv` 记录固定分区、阶段及父页。标签事实源是 `pipeline/common/events.py` 恢复的最终日志状态；TXT和进度文件是缓存。有同名 `.log` 时不再合并旧 `.jsonl`。数字／涂黑日志走 `row_log.py`，准入走 `cnn_data.py`。

新数据保留原图、来源和父页身份。正反面、重复扫描及其裁图不能跨训练／验证；原图不被转正输出覆盖。同名工作图不能换像素。预测和API结果保持待人审，不写成审核事件。

`pipeline/cascade/crop.py` 是整页裁M2入口，**不是M3导入器**。M3新卷导入需独立准备裁图、来源、固定分区和审核名单，核实当前数据结构后执行。不能通过复制识别输出或重新随机分区跳过这些步骤。

预标注入口为 `pipeline/m1/pred.py`、`pipeline/m2/pred.py`、`pipeline/m3/pred.py`。M2／M3用 `--subset` 限定本轮，M3显式指定 `--weights`；已有事件拒绝重打，不删除日志绕过。M3 GPU曾出现非有限值及有限值坏框，基础预标注入口没有自动跨设备guard，使用前先以CPU FP32参照检查，不关闭已有数值保护。

## 冻结、训练、比较、部署

1. 确定任务、目标错例和本轮审核名单。检测快照需要显式 `--stage`，并以 `--subset` 指定本轮必须核查的 `new_name`；subset不等于只训练本批，stage控制累计范围。
2. 先执行 `tools/迭代.py snapshot --task <任务> --run <新run名> --stage <阶段> --subset <CSV> --check-only`。核对人工状态、类别、边界、父页、分区、排除项和固定验证身份；通过后用同一组参数去掉 `--check-only` 冻结。CNN的digit／choice不传检测专用stage／subset参数。
3. 独立快照在 `train/<run>/dataset_snapshot`；不要复用共享dataset或覆盖旧run。现有数据的固定验证基线必须保留；公开新库需要明确建立自己的基线，不伪造作者历史名单。
4. 经统一 `train` 子命令训练，显式核实起点、架构、输入尺寸、轮数、学习率、batch和设备。部署包不含全部训练底座。不要用底层脚本默认配方替代已确定方案，不通过 `--scratch` 掩盖起点缺失。
5. 核对 `exit_status`、`completion_check`、`candidate` 及权重有限性和来源。人工停止单列退出事实，不改报自然完成；旧run缺记录时按实际情况处理。
6. 在同一名单、标签、预处理、阈值和设备口径上比较新旧；另保留新模板错例及旧卷回归。框指标和整题内容完整性都要看，不把固定验证选模成绩称为独立泛化测试。
7. 成功候选永久留档。展示实际效果，经用户明确同意才用 `tools/迭代.py deploy --task <任务> --run <run>`，同步部署注册表与别名。训练完成不自动换部署。

旧 `build_dataset.py`、CNN旧构建器等已退休，有停止保护。使用统一snapshot入口，不恢复旧写入或force绕过。`--range-aware` 是M3可选实验而非默认开关；改变训练方案先落实本轮授权和比较方法。

## 识别与测试

豆包默认Lite非思考、high、约1500方形拼图、4并发。独立图块ID映射原页和题目，同题号跨页面不合并；成功响应缓存，中断后复用。提示词语义改变后不能复用旧语义缓存。空白、看不清、含图舍弃、请求失败分别保留。当前规则为任何含图片／示意图的小题整题舍弃，其余小题继续。

识别反馈只写本次结果的 `feedback.json` 及历史，不自动进入训练库。测试审核功能使用临时假库，保护真实草稿。按改动选回归，入口见开发说明；例如：

```powershell
python -B pipeline/tests/test_choice_matching.py -v
python -B pipeline/tests/test_printed_number.py
python -B pipeline/tests/test_recognition.py --help
python -B pipeline/tests/test_m3_snapshot.py --help
```

测试是否需要临时目录、假库、模型和额外依赖，以实际脚本为准。声明清楚是模拟DOM、HTTP资源检查还是真实Edge交互；不凭模型输出自评手写准确率。

## 保存记录与发布

改文件前备份。作者本机临时产物统一放 `D:/temp/<上海时间YYYY-MM-DD-HH-mm>_codex_<用途>/`，不放项目临时目录；其他电脑用同等独立临时目录。不要动作者桌面777。长期训练run、数据与权重按项目角色存放。

本地存在 `docs/01-接力记录.txt` 时，仅用 `tools/写快照.py` 更新，结构3含当前状态、本轮限制、待办与阻塞、证据入口四节。先inspect、校验当前SHA、dry-run，再提交并读回；同窗口沿用session，普通更新不加handoff。用户要求换窗口才带handoff和event，核对archived及SHA后报告交接完成。不存在本地接力时无需恢复作者的私有历史，记录本轮实际产物入口即可。

公开内容限源码、通用文档、测试、加密部署权重和汇总。排除 `.env`、凭据、试卷、个人反馈、训练数据、逐图日志、本地接力和临时报告。发布前扫描待提交内容、核对单文件大小、文档链接和必要回归；权重变化才重打包，AES-256隐藏文件名、90 MiB分卷、全文件SHA解包核验，密码不写仓库。推送前核对远端新提交，普通推送，保留Git历史。
