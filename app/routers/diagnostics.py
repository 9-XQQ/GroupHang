"""仅登录用户可读的第三方服务诊断信息。"""
from fastapi import APIRouter, Depends

from ..models import User
from ..security import get_current_user
from ..services.llm_parser import llm_place_parser

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.get("/llm")
async def llm_diagnostics(_user: User = Depends(get_current_user)) -> dict:
    """返回进程内 LLM token/cache 汇总，不包含 Key、提示词或用户原文。"""
    return llm_place_parser.diagnostics()
