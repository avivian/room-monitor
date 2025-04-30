
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for smbus2 and other libraries
RUN apt-get update && apt-get install -y \
  i2c-tools \
  python3-smbus \
  build-essential \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY monitor.py .

# Run the application
CMD ["python", "monitor.py"]
