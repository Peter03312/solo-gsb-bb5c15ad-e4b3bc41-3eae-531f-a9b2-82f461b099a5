"""请求体模型与校验规则 (非法容量等在此明确报错)。"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, StrictInt


class PaperIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128)
    manuscript: str = ""
    topics: List[str] = Field(default_factory=list)
    institutions: List[str] = Field(default_factory=list)


class PaperUpdate(BaseModel):
    manuscript: str = ""
    topics: List[str] = Field(default_factory=list)
    institutions: List[str] = Field(default_factory=list)


class PaperWithdrawalIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128, description="要撤回的现存论文编号")
    base_revision: int = Field(ge=0, description="所见到的资料修订号; 与当前不一致即过期")
    reason: str = Field(min_length=1, max_length=1000, description="非空撤回原因 (首尾空白归一化后仍须非空)")


class PaperGuaranteeLevelIn(BaseModel):
    """按论文设置评审保障等级 (high/medium/normal; 未设置即普通, 设置为 normal 即恢复默认)。"""

    paper_id: str = Field(min_length=1, max_length=128, description="要设置保障等级的现存论文编号")
    level: str = Field(
        min_length=1, max_length=16,
        description="评审保障等级: high=高, medium=中, normal=普通 (其余值非法)",
    )
    base_revision: int = Field(ge=0, description="所见到的资料修订号; 与当前不一致即过期")


class ReviewerIn(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128)
    credential: str = Field(min_length=1, max_length=256)
    topics: List[str] = Field(default_factory=list)
    institution: str = Field(min_length=1, max_length=256)
    capacity: StrictInt = Field(ge=1, le=100000, description="最大可评审论文数, 必须为 >= 1 的整数")
    avoid_papers: List[str] = Field(default_factory=list)


class ReviewerUpdate(BaseModel):
    credential: str = Field(min_length=1, max_length=256)
    topics: List[str] = Field(default_factory=list)
    institution: str = Field(min_length=1, max_length=256)
    capacity: StrictInt = Field(ge=1, le=100000)
    avoid_papers: List[str] = Field(default_factory=list)


class PublishIn(BaseModel):
    base_revision: int = Field(ge=0, description="预演结果中返回的资料修订号")


class LockTableIn(BaseModel):
    """普通分配锁定表 (整表替换): 每篇论文 0~2 名锁定评审人。

    locks 缺省或为空对象表示清除全部锁定; 未列出的论文视为不锁定。
    提交时校验论文/评审人均存在且不重复, 但被锁评审人是否仍合格
    (停用/回避/机构冲突/容量) 由预演与发布时的求解器诊断, 提交不拒绝。
    """

    base_revision: int = Field(ge=0, description="所见到的资料修订号; 与当前不一致即过期")
    locks: dict[str, List[str]] = Field(
        default_factory=dict,
        description="{论文编号: [锁定评审人编号 ...]}, 每篇 0~2 名; 空表清除全部锁定",
    )


class AssignmentDecisionIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128, description="当前发布版中分配给本人的论文编号")
    decision: Literal["confirm", "recuse"] = Field(description="confirm=确认接受, recuse=声明回避")
    reason: Optional[str] = Field(default=None, max_length=1000, description="回避原因; recuse 时必须非空")


class BackfillPublishIn(BaseModel):
    base_revision: int = Field(ge=0, description="补位预演结果中返回的资料修订号")
    base_serial: int = Field(ge=0, description="补位预演所基于的发布序号; 期间发生过任何发布则过期")


class ReviewDeadlineIn(BaseModel):
    """会务方为当前发布版设置一次统一 UTC 评审截止时刻。

    必须同时携带当前资料修订号与当前发布序号; 截止时刻须显式携带时区
    (允许 Z / +00:00 等偏移, 服务端归一化为 UTC) 且严格晚于设置时刻。
    同版同时刻重试幂等 (changed=false, 不产生新变更); 同版异时刻拒绝 (409);
    版本不符 (base_revision/base_serial 过期或超前) 拒绝 (409);
    非法时刻 (无法解析/无时区/不晚于当前时刻) 拒绝 (422); 尚未发布 404。
    """

    base_revision: int = Field(ge=0, description="所见到的资料修订号; 与当前不一致即过期")
    base_serial: int = Field(ge=1, description="截止所针对的当前发布序号; 与当前不一致即过期")
    deadline_at: str = Field(
        min_length=1, max_length=64,
        description="统一 UTC 评审截止时刻 (ISO 8601, 须显式携带时区且晚于设置时刻)",
    )


class ReviewSubmissionIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128, description="当前发布版中分配给本人且已确认的论文编号")
    serial: int = Field(ge=1, description="提交所针对的发布序号; 与当前发布序号不一致即过期")
    score: StrictInt = Field(ge=1, le=5, description="1~5 的整数评分")
    comment: str = Field(min_length=1, max_length=10000, description="非空正式评语")


class SnapshotPublishIn(BaseModel):
    serial: int = Field(ge=1, description="要发布的反馈快照所依据的发布序号; 与当前发布序号不一致即过期")


class ReviewerStatusIn(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128, description="要调整资格的评审人编号")
    base_revision: int = Field(ge=0, description="所见到的资料修订号; 与当前不一致即过期")
    active: bool = Field(description="目标状态: false=停用, true=启用")
    reason: str = Field(min_length=1, max_length=1000, description="非空原因 (首尾空白归一化后仍须非空)")


class InstitutionMergeIn(BaseModel):
    name_a: str = Field(min_length=1, max_length=256, description="当前论文作者或评审人资料中出现的机构原名")
    name_b: str = Field(min_length=1, max_length=256, description="当前论文作者或评审人资料中出现的机构原名")
    base_revision: int = Field(ge=0, description="所见到的资料修订号; 与当前不一致即过期")


class ReviewCorrectionRequestIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128, description="需要更正评语的论文编号")
    serial: int = Field(ge=1, description="当前发布序号; 与当前发布序号不一致即过期")
    reviewer_id: str = Field(min_length=1, max_length=128, description="被更正评语所属的评审人编号")
    reason: str = Field(min_length=1, max_length=1000, description="非空更正原因 (首尾空白归一化后仍须非空)")


class ReviewCorrectionSubmitIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128, description="待更正任务对应的论文编号")
    serial: int = Field(ge=1, description="更正请求对应的发布序号; 与当前发布序号不一致即过期")
    original_receipt: str = Field(min_length=1, max_length=128, description="被更正评语的收据")
    score: StrictInt = Field(ge=1, le=5, description="更正后的 1~5 整数评分")
    comment: str = Field(min_length=1, max_length=10000, description="更正后的非空评语")


class FeedbackObjectionIn(BaseModel):
    """作者凭当前有效快照访问码对匿名标号 1/2 的评语提交异议。"""

    access_code: str = Field(min_length=1, max_length=128, description="会务方发放且当前有效的快照访问码 (fbk- 前缀)")
    label: StrictInt = Field(ge=1, le=2, description="异议所针对的匿名标号: 1 或 2")
    reason: str = Field(min_length=1, max_length=5000, description="非空异议理由 (首尾空白归一化后仍须非空)")


class FeedbackObjectionDecisionIn(BaseModel):
    """会务方对异议的处理决定: reject=驳回 (不影响快照), accept=受理 (走既有更正请求流程)。"""

    decision: Literal["reject", "accept"] = Field(description="reject=驳回, accept=受理")
    note: Optional[str] = Field(default=None, max_length=5000, description="驳回说明 (可选, 随驳回记录)")
    reason: Optional[str] = Field(
        default=None, max_length=1000,
        description="受理时必填的非空更正原因 (转交既有评语更正请求流程)",
    )
