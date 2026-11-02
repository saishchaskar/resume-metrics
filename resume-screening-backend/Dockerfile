# Use the official Python image.
# https://hub.docker.com/_/python
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Cloud Run provides PORT; keep the conventional local/container default.
ENV PORT=8080

# One process loads the embedding model once; threaded requests share it.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 900 app:app"]
