#!/bin/bash
set -e

# Configuration
REGISTRY="ghcr.io"
IMAGE_NAME="amanchoudhri/blogregator"
CACHE_TAG="buildcache"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Get version from argument or prompt
VERSION=${1:-}
if [ -z "$VERSION" ]; then
    echo -e "${YELLOW}Usage: ./build.sh <version>${NC}"
    echo -e "${YELLOW}Example: ./build.sh v1.0.0${NC}"
    exit 1
fi

# Validate version format
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo -e "${RED}Error: Version must be in format v1.2.3${NC}"
    exit 1
fi

# Extract version components for additional tags
MAJOR=$(echo "$VERSION" | sed 's/v\([0-9]*\).*/\1/')
MINOR=$(echo "$VERSION" | sed 's/v\([0-9]*\.[0-9]*\).*/\1/')
PATCH=$(echo "$VERSION" | sed 's/v//')

echo -e "${BLUE}🚀 Building blogregator ${VERSION}${NC}"
echo -e "${BLUE}   Tags: ${PATCH}, ${MINOR}, ${MAJOR}, latest${NC}"

# Check if logged in to GHCR
echo -e "${BLUE}🔐 Checking GHCR authentication...${NC}"
if ! docker login ghcr.io --get-login &>/dev/null; then
    echo -e "${YELLOW}Not logged in to GHCR. Logging in...${NC}"
    echo -e "${YELLOW}You'll need a GitHub Personal Access Token with 'write:packages' scope${NC}"
    echo -e "${YELLOW}Create one at: https://github.com/settings/tokens${NC}"
    docker login ghcr.io
fi

# Set up buildx builder if needed
BUILDER_NAME="blogregator-builder"
if ! docker buildx inspect "$BUILDER_NAME" &>/dev/null; then
    echo -e "${BLUE}🔧 Creating buildx builder...${NC}"
    docker buildx create --name "$BUILDER_NAME" --use --bootstrap
else
    docker buildx use "$BUILDER_NAME"
fi

# Build and push with multi-platform support
echo -e "${BLUE}📦 Building and pushing multi-platform image...${NC}"
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --tag "${REGISTRY}/${IMAGE_NAME}:${PATCH}" \
    --tag "${REGISTRY}/${IMAGE_NAME}:${MINOR}" \
    --tag "${REGISTRY}/${IMAGE_NAME}:${MAJOR}" \
    --tag "${REGISTRY}/${IMAGE_NAME}:latest" \
    --cache-from "type=registry,ref=${REGISTRY}/${IMAGE_NAME}:${CACHE_TAG}" \
    --cache-to "type=registry,ref=${REGISTRY}/${IMAGE_NAME}:${CACHE_TAG},mode=max" \
    --push \
    .

echo -e "${GREEN}✅ Successfully built and pushed:${NC}"
echo -e "${GREEN}   - ${REGISTRY}/${IMAGE_NAME}:${PATCH}${NC}"
echo -e "${GREEN}   - ${REGISTRY}/${IMAGE_NAME}:${MINOR}${NC}"
echo -e "${GREEN}   - ${REGISTRY}/${IMAGE_NAME}:${MAJOR}${NC}"
echo -e "${GREEN}   - ${REGISTRY}/${IMAGE_NAME}:latest${NC}"

# Optionally create and push git tag
echo ""
read -p "Create and push git tag ${VERSION}? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if git rev-parse "$VERSION" >/dev/null 2>&1; then
        echo -e "${YELLOW}Tag ${VERSION} already exists${NC}"
    else
        git tag "$VERSION"
        git push origin "$VERSION"
        echo -e "${GREEN}✅ Git tag ${VERSION} created and pushed${NC}"
    fi
fi

echo ""
echo -e "${GREEN}🎉 Done! Deploy with:${NC}"
echo -e "${GREEN}   ./deploy.sh ${VERSION}${NC}"
