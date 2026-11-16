#!/bin/sh
# ACID RADIO - Developed by acidvegas in Python (https://git.supernets.org/acidvegas/acid-radio)
# acid-radio/setup.sh

# Set xtrace, exit on error, & verbose mode
set -xev

# Ensure persistent mount targets exist
mkdir -p music data

# Remove the existing container, remembering its image so only that one is cleaned up after the rebuild
docker rm -f acid-radio 2>/dev/null || true
OLD_IMAGE=$(docker images -q acid-radio)

# Build the Docker image
docker build --no-cache -t acid-radio .
[ -n "$OLD_IMAGE" ] && [ "$OLD_IMAGE" != "$(docker images -q acid-radio)" ] && docker rmi "$OLD_IMAGE" || true

# Run the Docker container
docker run -d --name acid-radio --restart unless-stopped --user 10001:10001 -p 127.0.0.1:7000:7000 -v "$PWD/music:/app/music" -v "$PWD/data:/app/data" acid-radio
