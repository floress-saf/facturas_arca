"""Configuración de Gunicorn para Render."""
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8080')}"
workers = 2
threads = 2
timeout = 120
