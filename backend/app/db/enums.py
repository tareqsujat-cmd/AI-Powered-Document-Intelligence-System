from enum import StrEnum


class FileType(StrEnum):
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    TIFF = "tiff"
    XLSX = "xlsx"
    XLS = "xls"
    CSV = "csv"


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingStage(StrEnum):
    UPLOAD = "upload"
    FILE_VALIDATION = "file_validation"
    TEXT_EXTRACTION = "text_extraction"
    LLM_EXTRACTION = "llm_extraction"
    VALIDATION = "validation"
    INDEXING = "indexing"
    FINISHED = "finished"


class TextSource(StrEnum):
    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    SPREADSHEET = "spreadsheet"


class PageStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"


class ExtractionMethod(StrEnum):
    LLM = "llm"
    RULE_BASED = "rule_based"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ProcessingEvent(StrEnum):
    UPLOADED = "uploaded"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    REPROCESS_REQUESTED = "reprocess_requested"
    MANUAL_CORRECTION = "manual_correction"
