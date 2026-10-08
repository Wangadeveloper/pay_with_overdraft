#!/usr/bin/env python3
"""
PayFlow Overdraft — Development Server Entry Point
"""
import os
from dotenv import load_dotenv

load_dotenv()

from app import create_app

app = create_app()

if __name__ == '__main__':
    print("⚡ PayFlow Overdraft — Starting Flask Dev Server")
    print("🌐 Open: http://localhost:5000")
    print("📊 Dashboard: http://localhost:5000/dashboard")
    print("🛒 Checkout: http://localhost:5000/checkout")
    print("💳 Overdraft: http://localhost:5000/overdraft")
    print("🤖 AI Chat: http://localhost:5000/ai")
    print("─" * 50)
    app.run(
        debug=True,
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000))
    )
