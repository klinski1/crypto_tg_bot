from aws_cdk import App
from stack import CryptoBotStack
app = App()
CryptoBotStack(app, "CryptoBot")
app.synth()
