import os
import sys
import subprocess
import argparse
import tempfile
from pathlib import Path

def fetch_audio(url, out_path):
    print(f"Downloading audio from {url}...")
    subprocess.run([
        'yt-dlp', 
        '-x', 
        '--audio-format', 'mp3',
        '--audio-quality', '5', # 192kbps approx
        '-o', out_path,
        url
    ], check=True)
    print(f"Audio downloaded to {out_path}")

def transcribe_audio_whisper(audio_path, api_key):
    import httpx
    print("Transcribing with Whisper API (OpenAI)...")
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": open(audio_path, "rb")}
    data = {"model": "whisper-1"}
    
    with httpx.Client(timeout=600.0) as client:
        response = client.post(url, headers=headers, files=files, data=data)
        response.raise_for_status()
        return response.json()["text"]

def main():
    parser = argparse.ArgumentParser(description="Fetch and transcribe YouTube/RuTube video")
    parser.add_argument("url", help="URL of the video")
    parser.add_argument("-o", "--output", help="Output transcript file path", default="docs/transcripts/raw_transcript.txt")
    parser.add_argument("--api-key", help="OpenAI API Key (or set OPENAI_API_KEY env var)", default=os.getenv("OPENAI_API_KEY"))
    
    args = parser.parse_args()
    if not args.api_key:
        print("Error: Missing OpenAI API Key")
        sys.exit(1)
        
    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        
    with tempfile.TemporaryDirectory() as tmp_dir:
        audio_file = os.path.join(tmp_dir, "audio.mp3")
        fetch_audio(args.url, audio_file)
        
        transcript = transcribe_audio_whisper(audio_file, args.api_key)
        
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(transcript)
            
        print(f"Transcript saved to {args.output}")

if __name__ == "__main__":
    main()
