# ── Estágio de build ────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Instala dependências em camada separada para melhor uso do cache
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Estágio final ────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copia apenas os pacotes instalados do estágio de build
COPY --from=builder /root/.local /root/.local

# Copia o código da aplicação
COPY app.py .

# Garante que os binários do pip --user estão no PATH
ENV PATH=/root/.local/bin:$PATH

# Usuário não-root para segurança
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 8005

# Roda com gunicorn em produção
CMD ["gunicorn", "--bind", "0.0.0.0:8005", "--workers", "2", "--timeout", "120", "app:app"]
