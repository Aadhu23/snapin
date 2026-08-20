# Use official lightweight Python image
FROM python:3.9-slim

# Set working directory inside the container
WORKDIR /app

# Install system dependencies required for OpenCV
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY backend/ ./backend/
COPY static/ ./static/

# The yolo model will be downloaded automatically on first run, 
# or you can copy it if you have it locally:
# COPY yoloWeights.pt . 

# Expose the API port
EXPOSE 8000

# Command to run the server
CMD ["python", "backend/server.py"]
