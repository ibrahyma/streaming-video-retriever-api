import json
import os.path
import shutil
import subprocess
import traceback
import urllib.parse
import ffmpeg

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from spleeter.separator import Separator
from starlette.responses import JSONResponse
from yt_dlp import YoutubeDL



def clear_directory(path):
    if not os.path.exists(path): return
    shutil.rmtree(path)
    os.makedirs(path)



def get_file_name(file: str):
    filename_parts = file.split('.')
    filename_parts.pop()
    return "".join(filename_parts)



def download_source_videos(url):
    ydl_opts = {
        'no_warnings': True,
        'noplaylist': True,
        'outtmpl': f'{TEMP_VIDEOS_FOLDER}/%(title)s.%(ext)s',
        'external_downloader': 'aria2c',
        'external_downloader_args': ['-x8', '-k1M'],
        'skip_download': False,
        'no_call_home': True,
        'no_check_certificate': True,
        'socket_timeout': 3,
        'source_address': '0.0.0.0',
        'force_generic_extractor': False,
        'cachedir': False,
        'prefer_ffmpeg': True,
        'postprocessors': [],
    }

    with YoutubeDL(ydl_opts) as ydl:
        print("Attempt to download source videos")
        ydl.download([url])

    return {"videos": os.listdir(TEMP_VIDEOS_FOLDER)}



def process_video_synchronously(temp_video_filename):
    temp_audio_filename = os.path.splitext(temp_video_filename)[0] + '.wav'
    temp_video_file_path = os.path.join(TEMP_VIDEOS_FOLDER, temp_video_filename)
    temp_audio_file_path = os.path.join(TEMP_AUDIO_FOLDER, temp_audio_filename)

    print("extract audio from temp video")
    extract_audio_from_video(temp_video_file_path, temp_audio_file_path)

    print("extract voice from temp audio")
    vocals_path = extract_voice_from_audio(temp_audio_file_path)

    print("final step...")
    concat_video_with_audio(temp_video_file_path, vocals_path)

    print(f"converted {temp_video_file_path}")



def extract_audio_from_video(video_path, audio_path):
    ffmpeg.input(video_path).output(
        audio_path,
        vn=None,
        acodec='pcm_s16le',
        ar=44100,
        ac=2
    ).run()



def extract_voice_from_audio(file_path):
    separator = Separator("spleeter:2stems")
    separator.separate_to_file(file_path, TEMP_AUDIO_CONVERTED_FOLDER)
    return os.path.join(
        TEMP_AUDIO_CONVERTED_FOLDER,
        os.path.splitext(os.path.basename(file_path))[0],
        "vocals.wav"
    )



def concat_video_with_audio(video_path, vocals_path):
    video_filename = os.path.splitext(os.path.basename(video_path))[0]
    output_video = os.path.join(OUTPUT_VIDEOS_FOLDER, video_filename + ".mp4")

    ffmpeg.output(
        ffmpeg.input(video_path).video,
        ffmpeg.input(vocals_path).audio,
        output_video,
        vcodec='copy',
        acodec='aac',
        shortest=None
    ).run()




def reset_storage(with_output: bool = False):
    os.makedirs(TEMP_VIDEOS_FOLDER, exist_ok=True)
    os.makedirs(TEMP_AUDIO_FOLDER, exist_ok=True)
    os.makedirs(TEMP_AUDIO_CONVERTED_FOLDER, exist_ok=True)
    os.makedirs(OUTPUT_VIDEOS_FOLDER, exist_ok=True)
    clear_directory(TEMP_VIDEOS_FOLDER)
    clear_directory(TEMP_AUDIO_FOLDER)
    clear_directory(TEMP_AUDIO_CONVERTED_FOLDER)
    if with_output:
        clear_directory(OUTPUT_VIDEOS_FOLDER)



def _convert_callback(request: Request, url: str):
    reset_storage(True)

    print("downloading file")

    download_source_videos(url)

    if len(os.listdir(TEMP_VIDEOS_FOLDER)) == 0:
        raise HTTPException(status_code=404, detail="Video not found")

    print(f"finded videos {os.listdir(TEMP_VIDEOS_FOLDER)}")

    for temp_video_filename in os.listdir(TEMP_VIDEOS_FOLDER):
        process_video_synchronously(temp_video_filename)

    videos = []
    base_url = str(request.base_url)

    for file in os.listdir(OUTPUT_VIDEOS_FOLDER):
        encoded_filename = urllib.parse.quote(file)
        videos.append({
            "filename": get_file_name(file),
            "url": f"{base_url}files/{encoded_filename}"
        })

    return JSONResponse(status_code=201, content={ "videos": videos, "error": None })



def _get_video_data_callback(url):
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "-J", url],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        videos = []

        print("videos", data)

        for entry in data.get("entries", []):
            video_id = entry.get("id")
            title = entry.get("title", "Sans titre")
            video = {
                "title": title, "video_id": video_id
            }
            videos.append(video)

        return videos

    except subprocess.CalledProcessError as e:
        print("Erreur yt-dlp:", e.stderr)
        return []



TEMP_VIDEOS_FOLDER = 'temp_video'
TEMP_AUDIO_FOLDER = 'temp_audio'
TEMP_AUDIO_CONVERTED_FOLDER = 'temp_audio_converted'
OUTPUT_VIDEOS_FOLDER = 'output'

os.makedirs(OUTPUT_VIDEOS_FOLDER, exist_ok=True)

app = FastAPI()

app.mount("/files", StaticFiles(directory=OUTPUT_VIDEOS_FOLDER), name="output_files")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)



# TODO: Un nouvel appel de la même requête doit pouvoir interrompre l'appel précédent en cours et réinitialiser son état
## Approche à tenter : arrêter l'exécution de la fonction en cours, attendre quelques secondes, en exécuter une nouvelle
@app.post("/convert")
async def convert_endpoint(request: Request, url: str):
    try:
        if url.startswith(request.base_url.__str__()):
            raise HTTPException(status_code=400, detail="Url must not be server")
        print(url, f"/endpoint called")
        result = _convert_callback(request, url)
        print(url, "Done")
        return result
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"videos": [], "error": str(e)})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"videos": [], "error": str(e)})



@app.get("/videos-data")
async def get_videos_data_endpoint(request: Request, url: str):
    try:
        if url.startswith(request.base_url.__str__()):
            raise HTTPException(status_code=400, detail="Url must not be server")
        print(url, f"/endpoint called")
        result = _get_video_data_callback(url)
        print(url, "Done")
        return result
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"data": [], "error": str(e)})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"data": [], "error": str(e)})



if __name__ == 'main':
    print("starting server...")
    reset_storage(True)
