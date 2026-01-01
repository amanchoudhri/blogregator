#!/bin/bash
# Start the Blogregator server

set -e

echo "Starting Blogregator server..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "Error: .env file not found!"
    echo "Please create a .env file with required environment variables."
    exit 1
fi

# Build and start the container
docker-compose up -d --build

# Wait for health check
echo "Waiting for server to be healthy..."
sleep 10

# Check health
if curl -f http://localhost:8000/health > /dev/null 2>&1; then
    echo "✓ Blogregator server is running!"
    echo "Dashboard: http://localhost:8000"
    echo "Logs: docker-compose logs -f"
else
    echo "✗ Server health check failed"
    echo "Check logs with: docker-compose logs"
    exit 1
fi
