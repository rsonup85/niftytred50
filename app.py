from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Render Flask Deployment Successful!"

if __name__ == '__main__':
    app.run()