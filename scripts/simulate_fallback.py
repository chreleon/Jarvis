import sys
from pathlib import Path

# Ensure project root is on sys.path when running from scripts/ directory
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from or_client import client
# Backup
orig_groq = client._groq_client
orig_github = client._github_models_request

class FakeGroq:
    class chat:
        class completions:
            @staticmethod
            def create(*args, **kwargs):
                raise Exception("401 Unauthorized: invalid token")

def fake_groq_client():
    return FakeGroq()

def fake_github_request(payload):
    return {"choices":[{"message":{"content":"FALLBACK OK"}}]}

client._groq_client = fake_groq_client
client._github_models_request = fake_github_request

print('Provider before:', client.provider)
res = client.chat('Hello, how are you?')
print('Chat result:', res)
print('Provider after:', client.provider)

# Restore
client._groq_client = orig_groq
client._github_models_request = orig_github
