from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.cnn_extractor import CNNExtractor
    from services.cache_service import CacheService
    from services.drive_service import DriveService
    from services.ims_service import IMSService
    from services.openai_service import OpenAIService
    from services.whatsapp_service import WhatsAppService

cnn: "CNNExtractor" = None
cache: "CacheService" = None
drive: "DriveService" = None
wa: "WhatsAppService" = None
openai_svc: "OpenAIService" = None
ims: "IMSService" = None
