#!/bin/bash
# Run this after deploying Railway to bake the Railway URL into the frontend.
# Usage: ./build_and_copy_frontend.sh https://your-app.up.railway.app

RAILWAY_URL=${1:-""}

if [ -z "$RAILWAY_URL" ]; then
  echo "Usage: ./build_and_copy_frontend.sh https://your-app.up.railway.app"
  exit 1
fi

echo "Building frontend with VITE_API_URL=$RAILWAY_URL"

cd ../frontend
VITE_API_URL=$RAILWAY_URL npm run build

echo "Copying dist to railway/dist..."
rm -rf ../railway/dist
cp -r dist ../railway/dist

echo "Done. Now redeploy Railway (git push or railway up)."
