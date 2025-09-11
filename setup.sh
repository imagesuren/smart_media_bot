```bash
#!/bin/bash
# Smart Media + Knowledge Bot Setup Script

echo "🤖 Setting up Smart Media + Knowledge Bot..."

# Check Python version
echo "📋 Checking Python version..."
python_version=$(python3 --version 2>&1)
echo "   Found: $python_version"

if python3 -c 'import sys; exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "   ✅ Python version is compatible"
else
    echo "   ❌ Python 3.8+ required. Please upgrade Python."
    exit 1
fi

# Create virtual environment
echo "🔧 Creating virtual environment..."
python3 -m venv venv
echo "   ✅ Virtual environment created"

# Activate virtual environment
echo "🔌 Activating virtual environment..."
source venv/bin/activate
echo "   ✅ Virtual environment activated"

# Upgrade pip
echo "📦 Upgrading pip..."
pip install --upgrade pip
echo "   ✅ Pip upgraded"

# Install requirements
echo "📥 Installing requirements..."
pip install -r requirements.txt
echo "   ✅ Requirements installed"

# Create downloads folder
echo "📁 Creating downloads folder..."
mkdir -p downloads
echo "   ✅ Downloads folder created"

echo ""
echo "🎉 Setup completed successfully!"
echo ""
echo "📝 Next steps:"
echo "   1. Get your bot token from @BotFather on Telegram"
echo "   2. Add your token to the .env file"
echo "   3. Run: python smart_media_bot.py"
echo ""
echo "💡 For free hosting, see the deployment guide"
```