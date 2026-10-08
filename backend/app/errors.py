class PipelineStageError(RuntimeError):
    """包含处理阶段信息的工作流异常。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
