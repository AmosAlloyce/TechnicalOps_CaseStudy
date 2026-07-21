FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scale_sim.py .
EXPOSE 8502
CMD ["streamlit", "run", "scale_sim.py", "--server.port=8502", "--server.address=0.0.0.0"]
