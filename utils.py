import re
from pymediainfo import MediaInfo

def extract_video_metadata(caption):
    """استخلاص البيانات الوصفية من كابشن الفيديو."""
    metadata = {"qualities": [], "is_translated": False, "is_dubbed": False}
    if not caption:
        return metadata

    # استخلاص الجودات
    quality_patterns = {
        "1080p": [r"1080[pP]", r"FHD", r"Full\\s*HD"],
        "720p": [r"720[pP]", r"\\bHD\\b"],
        "480p": [r"480[pP]", r"\\bSD\\b"]
    }
    
    found_qualities = set()
    for res, patterns in quality_patterns.items():
        for pattern in patterns:
            if re.search(pattern, caption, re.IGNORECASE):
                found_qualities.add(res)
    
    # إضافة الجودات المكتشفة
    for quality in found_qualities:
        metadata["qualities"].append({"resolution": quality, "message_id": None})

    # استخلاص حالة الترجمة/الدبلجة
    if re.search(r"مترجم|sub|subbed|subtitle", caption, re.IGNORECASE):
        metadata["is_translated"] = True
    if re.search(r"مدبلج|dub|dubbed|arabic", caption, re.IGNORECASE):
        metadata["is_dubbed"] = True

    return metadata

def get_video_info(file_path):
    """استخلاص معلومات الفيديو باستخدام MediaInfo."""
    try:
        media_info = MediaInfo.parse(file_path)
        video_track = None
        for track in media_info.tracks:
            if track.track_type == \'Video\':
                video_track = track
                break
        
        if video_track:
            duration_ms = video_track.duration # in milliseconds
            duration_seconds = duration_ms / 1000 if duration_ms else 0
            
            # تنسيق المدة إلى H:MM:SS
            hours = int(duration_seconds // 3600)
            minutes = int((duration_seconds % 3600) // 60)
            seconds = int(duration_seconds % 60)
            formatted_duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"

            return {
                "duration": formatted_duration,
                "width": video_track.width,
                "height": video_track.height,
                "file_size": video_track.file_size, # in bytes
                "codec": video_track.codec,
                "frame_rate": video_track.frame_rate,
                "overall_bit_rate": video_track.overall_bit_rate,
                "display_aspect_ratio": video_track.display_aspect_ratio,
                "quality_resolution": f"{video_track.height}p" if video_track.height else "N/A"
            }
        return None
    except Exception as e:
        print(f"Error extracting video info: {e}")
        return None


