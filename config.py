# Copyright (c) 2024 PJSC VimpelCom
"""Конфигурация сервиса и подключения к БД."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "myuser"
    db_password: str = "mysecretpassword"
    db_name: str = "mydatabase"
    scripts_dir: str = "scripts"
    # Базовый URL API для скриптов проверок (POST /api/v1/product/{alias}/ff)
    api_base_url: str = "http://127.0.0.1:8000"
    # Таймаут HTTP POST при вызове внешней проверки (fitness_function.method)
    external_ff_timeout_seconds: float = 30.0
    # Базовый URL FDM Products API (GET /api/v1/product/{app}) — получение structurizrApiKey/Secret
    fdm_product_api_base_url: str = (
        "https://eafdmmart-develop-fdm-products.apps.yd-m6-kt22.vimpelcom.ru"
    )
    fdm_product_api_timeout_seconds: float = 30.0
    # Базовый URL API Structurizr для исходящих запросов из скриптов (HMAC)
    structurizr_http_base_url: str = ""
    structurizr_http_timeout_seconds: float = 30.0
    # Базовый URL Documents API (GET /api/v1/documents/{document_id})
    documents_api_base_url: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    class Config:
        env_prefix = "FF_"
        env_file = ".env"


settings = Settings()
