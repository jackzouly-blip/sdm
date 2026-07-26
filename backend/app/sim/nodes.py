"""编排节点类型注册表。

编辑器**不认识任何具体节点类型**：每个类型自行声明 params_schema（JSON Schema）
与输入输出端口，前端据此渲染参数表单。新增类型 = 注册一条定义，编辑器零改动。
这与 vektor3d capability registry 的 register({id, name, inputSchema, outputSchema})
是同一模式，也是"可视化编辑器不会把仍在演进的模型固化住"的化解办法。

执行器的返回值只有三种形态：

    Done(outputs)        进程内直接算完
    Waiting(ref, hint)   已派发，等外部完成——HPC 作业、浏览器代理调用的
                         vektor3d 能力、人工确认，三者机制相同：先挂起，
                         再由 complete_node 接口带结果回来推进
    Failed(message)      本节点失败，整个 run 失败

Waiting 是这套编排的核心：SDM 是编排者不是执行者，绝大多数节点的实际执行都在
进程外（PBS 集群、用户桌面的 vektor3d）。把"等外部"做成一等状态，而不是让执行器
阻塞线程去轮询，是这个引擎能用少量线程扛住大量长任务的原因。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..logger import get_logger

log = get_logger(__name__)


# --- 执行结果 -----------------------------------------------------------

@dataclass
class Done:
    outputs: Dict = field(default_factory=dict)


@dataclass
class Waiting:
    """已派发，等外部完成。

    ref: 外部句柄（HPC 作业号 / 能力作业号 / 人工确认标记），供回流时定位。
    hint: 给用户看的一句话，说明在等什么。
    """
    ref: Optional[str] = None
    hint: str = ""


@dataclass
class Failed:
    message: str


Outcome = "Done | Waiting | Failed"


@dataclass
class NodeContext:
    """执行器可用的上下文。

    inputs 是上游节点 outputs 的合并结果；params 是该节点在 DAG 文档里配置的参数。
    """
    run_id: str
    node_id: str
    params: Dict
    inputs: Dict
    owner: str
    sim_project_id: Optional[str]
    sim_subject_id: Optional[str]
    db: object  # SimDB，避免循环导入故不标注具体类型


@dataclass
class NodeType:
    type_id: str
    label: str
    # internal / capability / hpc / manual —— 决定节点在画布上的分组与配色
    category: str
    description: str
    params_schema: Dict
    inputs: List[str]
    outputs: List[str]
    executor: Callable[[NodeContext], object]

    def to_public(self) -> Dict:
        """给前端的声明（不含执行器）。画布的节点面板与参数表单据此生成。"""
        return {
            "type_id": self.type_id,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "params_schema": self.params_schema,
            "inputs": self.inputs,
            "outputs": self.outputs,
        }


_REGISTRY: Dict[str, NodeType] = {}


def register_node_type(nt: NodeType) -> NodeType:
    if nt.type_id in _REGISTRY:
        raise ValueError(f"节点类型已注册: {nt.type_id}")
    _REGISTRY[nt.type_id] = nt
    return nt


def get_node_type(type_id: str) -> Optional[NodeType]:
    return _REGISTRY.get(type_id)


def list_node_types() -> List[NodeType]:
    return sorted(_REGISTRY.values(), key=lambda n: (n.category, n.type_id))


# --- 内置节点类型 -------------------------------------------------------

def _exec_manual(ctx: NodeContext):
    """人工确认门。

    默认不用——SDM 编排以自动化为先，这与 dbit 向导的人工步进模型正相反。
    但保留它有实际价值：能力尚未就绪时（如 vektor3d 网格能力），先用人工节点占位，
    等能力到位换成 capability 节点，DAG 定义只改一处。
    """
    return Waiting(ref=None, hint=ctx.params.get("prompt") or "等待人工确认")


def _exec_validate_config(ctx: NodeContext):
    """按工况模板的 validation_rules 校验 sim_subject.config。

    规则形如 {"字段": {"required": true, "min": 0, "max": 120, "enum": [...]}}。
    刻意保持朴素：这层的价值是"把判据固化下来"，不是做一个规则引擎。
    """
    db = ctx.db
    sid = ctx.sim_subject_id
    if not sid:
        return Failed("该运行未绑定工况，无法校验配置")
    subject = db.get_subject(sid)
    if subject is None:
        return Failed("工况不存在")
    if not subject["template_id"]:
        return Failed("工况未绑定模板，无校验规则可用")
    tpl = db.get_template(subject["template_id"])
    if tpl is None:
        return Failed("工况绑定的模板已不存在")

    try:
        config = json.loads(subject["config_json"] or "{}")
        rules = json.loads(tpl["validation_rules_json"] or "{}")
    except ValueError as e:
        return Failed(f"配置或规则不是合法 JSON: {e}")

    problems: List[str] = []
    for key, rule in (rules or {}).items():
        if not isinstance(rule, dict):
            continue
        present = key in config and config[key] is not None
        if rule.get("required") and not present:
            problems.append(f"{key}: 必填项缺失")
            continue
        if not present:
            continue
        val = config[key]
        if "enum" in rule and val not in rule["enum"]:
            problems.append(f"{key}: 取值 {val!r} 不在允许集合 {rule['enum']} 内")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if "min" in rule and val < rule["min"]:
                problems.append(f"{key}: {val} 小于下限 {rule['min']}")
            if "max" in rule and val > rule["max"]:
                problems.append(f"{key}: {val} 超过上限 {rule['max']}")

    if problems:
        return Failed("配置校验未通过：" + "；".join(problems))
    return Done({"validated_config": config, "checked": len(rules or {})})


def _exec_export_deck(ctx: NodeContext):
    """按模板 export_mapping 把工况配置渲染成求解器输入卡文本。

    映射形如 {"template": "*VELOCITY\\n{velocity}\\n"}，用配置值做字符串替换。
    这一步刻意只产出文本、不落盘：写到哪由下游的 HPC 节点决定，
    避免这个节点同时承担"生成"与"放置"两件事。
    """
    db = ctx.db
    sid = ctx.sim_subject_id
    if not sid:
        return Failed("该运行未绑定工况，无法生成输入卡")
    subject = db.get_subject(sid)
    if subject is None or not subject["template_id"]:
        return Failed("工况不存在或未绑定模板")
    tpl = db.get_template(subject["template_id"])
    if tpl is None:
        return Failed("工况绑定的模板已不存在")

    try:
        config = json.loads(subject["config_json"] or "{}")
        mapping = json.loads(tpl["export_mapping_json"] or "{}")
    except ValueError as e:
        return Failed(f"配置或映射不是合法 JSON: {e}")

    text = mapping.get("template")
    if not text:
        return Failed("模板未配置 export_mapping.template，无法生成输入卡")
    # 上游节点的输出也参与替换（如网格节点产出的 mesh_file）
    values = {**config, **{k: v for k, v in ctx.inputs.items() if isinstance(v, (str, int, float))}}
    try:
        rendered = text.format(**values)
    except KeyError as e:
        return Failed(f"输入卡模板引用了配置中不存在的字段: {e}")
    return Done({"deck_text": rendered, "deck_bytes": len(rendered.encode("utf-8"))})


def _exec_capability(ctx: NodeContext):
    """调用外部能力服务（vektor3d 等）。

    vektor3d 是桌面端、监听 localhost:23710，SDM 后端**永远调不到它**，
    故此处不发起网络调用，而是把节点挂起、等浏览器代理完成后回流。
    浏览器侧从待办接口取走此节点、调本机 vektor3d、再带结果调 complete。

    将来若部署无头 vektor3d 节点，只需在这里改为直接发起调用，
    DAG 定义与前端都不用动。
    """
    cap = ctx.params.get("capability_id")
    if not cap:
        return Failed("未指定 capability_id")
    return Waiting(ref=None, hint=f"等待浏览器代理调用能力 {cap}")


def _exec_hpc_submit(ctx: NodeContext):
    """提交到 HPC 并等待求解完成。

    同样是"派发后等外部"：提交走现有 HPC 链路，完成由现有作业轮询回流。
    本期先建立挂起语义与外部句柄，与提交链路的接线在下一期完成——
    届时只改这个执行器，DAG 定义与引擎都不动。
    """
    return Waiting(ref=None, hint="等待提交并求解完成")


def register_builtin_node_types() -> None:
    """注册内置节点类型。幂等：重复调用不会重复注册。"""
    if _REGISTRY:
        return

    register_node_type(NodeType(
        type_id="internal.validate_config",
        label="配置校验",
        category="internal",
        description="按工况模板的 validation_rules 校验配置，不通过则中止整条流水线",
        params_schema={"type": "object", "properties": {}},
        inputs=[], outputs=["validated_config"],
        executor=_exec_validate_config,
    ))

    register_node_type(NodeType(
        type_id="internal.export_deck",
        label="生成输入卡",
        category="internal",
        description="按模板 export_mapping 将工况配置渲染为求解器输入卡文本",
        params_schema={"type": "object", "properties": {}},
        inputs=["validated_config"], outputs=["deck_text"],
        executor=_exec_export_deck,
    ))

    register_node_type(NodeType(
        type_id="capability.invoke",
        label="调用能力",
        category="capability",
        description="调用 vektor3d 等能力服务（经浏览器代理）。网格生成/检查等能力就绪后用此节点",
        params_schema={
            "type": "object",
            "properties": {
                "capability_id": {
                    "type": "string",
                    "title": "能力 ID",
                    "description": "如 mesh.generate / mesh.check / geometry.prepare",
                },
                "input": {
                    "type": "object",
                    "title": "调用输入",
                    "description": "透传给能力的输入对象",
                },
            },
            "required": ["capability_id"],
        },
        inputs=[], outputs=["result"],
        executor=_exec_capability,
    ))

    register_node_type(NodeType(
        type_id="hpc.submit",
        label="提交求解",
        category="hpc",
        description="提交到 PBS 集群并等待求解完成，完成状态由现有作业轮询回流",
        params_schema={
            "type": "object",
            "properties": {
                "queue": {"type": "string", "title": "队列", "default": "batch"},
                "nodes": {"type": "string", "title": "资源", "default": "1:ppn=8"},
            },
        },
        inputs=["deck_text"], outputs=["hpc_jobid"],
        executor=_exec_hpc_submit,
    ))

    register_node_type(NodeType(
        type_id="manual.confirm",
        label="人工确认",
        category="manual",
        description="挂起等待人工确认。默认不用——也可作为能力就绪前的占位节点",
        params_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "title": "提示语",
                           "description": "告诉操作者这一步要确认什么"},
            },
        },
        inputs=[], outputs=["confirmed_by"],
        executor=_exec_manual,
    ))

    log.info("已注册 %d 个内置编排节点类型", len(_REGISTRY))
