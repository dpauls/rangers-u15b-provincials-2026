#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "Setting up Rangers U15B Provincials tracker..."

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Check for API key
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo ""
    echo "WARNING: ANTHROPIC_API_KEY not set in environment."
    echo "Add to ~/.bashrc:  export ANTHROPIC_API_KEY='your-key-here'"
    echo "The site will still work with --skip-narrative flag."
else
    echo "ANTHROPIC_API_KEY is configured."
fi

# Verify git push works
echo ""
echo "Testing git remote..."
git remote -v

echo ""
echo "Setup complete! To run:"
echo "  source venv/bin/activate"
echo "  python3 src/generate.py --skip-narrative"
echo "  # or: ./scripts/update.sh --skip-narrative"
