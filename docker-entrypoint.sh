#!/bin/sh
set -e

# Create directories if they don't exist
mkdir -p /app/downloads /app/config /app/config/python-packages /app/config/bin

# Set proper permissions
chmod 755 /app/downloads /app/config /app/config/python-packages /app/config/bin

# Note: Config file will be created automatically by the Python application
# This ensures consistency between the entrypoint script and Python default config
echo "Directories prepared. Config files will be created by the application if needed."

# Set timezone to China Standard Time
export TZ=Asia/Shanghai

# Ensure yt-dlp can access the network properly
echo "Container initialized successfully. Starting application..."

# Execute the command
exec "$@"
