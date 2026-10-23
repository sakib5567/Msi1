import os
from flask import Flask

app = Flask(__name__)


@app.route('/')
def home():
  return 'Bot is running!'


if __name__ == '__main__':
  # Render-এর দেওয়া PORT ধরতে হবে, না পেলে ডিফল্ট ৫০০০ ব্যবহার করবে
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port)