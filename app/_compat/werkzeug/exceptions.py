"""werkzeug.exceptions compatible module."""
class HTTPException(Exception):
    def __init__(self, description="HTTP Error", code=500):
        super().__init__(description)
        self.description = description
        self.code = code
