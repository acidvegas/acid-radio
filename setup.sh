#!/bin/sh
# ACID RADIO - Developed by acidvegas in Python (https://git.supernets.org/acidvegas/acid-radio)
# acid-radio/setup.sh

# Set xtrace, exit on error, & verbose mode
set -xev

# Ensure persistent mount targets exist
mkdir -p music data

# Remove existing docker container and clean up old images/cache
docker rm -f acid-radio 2>/dev/null || true
docker system prune -af

# Build the Docker image
docker build --no-cache -t acid-radio .

# Run the Docker container
docker run -d --name acid-radio --restart unless-stopped -p 127.0.0.1:7000:7000 -v "$PWD/music:/app/music" -v "$PWD/data:/app/data" acid-radio
