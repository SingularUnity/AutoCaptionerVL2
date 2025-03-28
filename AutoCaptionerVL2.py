import torch
from transformers import AutoModelForCausalLM, BitsAndBytesConfig
from deepseek_vl2.models import DeepseekVLV2Processor
from deepseek_vl2.utils.io import load_pil_images
from PIL import Image
import cv2, os, ffmpeg
from accelerate import infer_auto_device_map, init_empty_weights

model_path = "deepseek-ai/deepseek-vl2-tiny"
processor = DeepseekVLV2Processor.from_pretrained(model_path)
tokenizer = processor.tokenizer

bnb_config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16)

with init_empty_weights():
    vl_gpt = AutoModelForCausalLM.from_pretrained(model_path, trust_remote_code=True)

device_map = infer_auto_device_map(vl_gpt, max_memory={0: '14GiB', 'cpu': '32GiB'}, no_split_module_classes=['DeepseekVLAttention'])
vl_gpt = AutoModelForCausalLM.from_pretrained(
    model_path,
    device_map=device_map,
    quantization_config=bnb_config,
    trust_remote_code=True
).eval()

def resize_image(input_path, output_path, max_resolution=(1280, 720)):
    if os.path.exists(output_path):
        return output_path
    img = Image.open(input_path)
    img.thumbnail(max_resolution, Image.Resampling.LANCZOS)
    img.save(output_path)
    return output_path

def resize_and_extract_video_frames(input_video, resized_video_path, frames_folder, max_size=456, fps_divisor=16, max_frames=49):
    if not os.path.exists(resized_video_path):
        probe = ffmpeg.probe(input_video)
        video_stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        width, height = int(video_stream['width']), int(video_stream['height'])

        new_w, new_h = (max_size, int(height * max_size / width)) if width > height else (int(width * max_size / height), max_size)

        ffmpeg.input(input_video).filter('scale', new_w, new_h).output(resized_video_path).run(overwrite_output=True)

    cap = cv2.VideoCapture(resized_video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_interval = fps_divisor
    possible_frames = total_frames // frame_interval

    options = sorted([49, 57, 65, 73, 81])
    while max_frames > possible_frames and max_frames > options[0]:
        options.remove(max_frames)
        max_frames = options[-1]

    start_frame = max((total_frames - max_frames * frame_interval) // 2, 0)
    extracted = 0

    os.makedirs(frames_folder, exist_ok=True)
    for i in range(total_frames):
        ret, frame = cap.read()
        if not ret:
            break
        if i >= start_frame and (i - start_frame) % frame_interval == 0 and extracted < max_frames:
            frame_path = os.path.join(frames_folder, f"frame_{extracted:03d}.jpg")
            cv2.imwrite(frame_path, frame)
            extracted += 1
    cap.release()

def generate_caption(images):
    conversation = [{"role": "<|User|>", "content": "\n".join([f"This is image_{idx+1}: <image>" for idx in range(len(images))]) + "\nDescribe these images briefly.", "images": images}, {"role": "<|Assistant|>", "content": ""}]

    pil_images = load_pil_images(conversation)
    inputs = processor(conversations=conversation, images=pil_images, force_batchify=True).to(vl_gpt.device)
    inputs_embeds = vl_gpt.prepare_inputs_embeds(**inputs)

    outputs = vl_gpt.language.generate(inputs_embeds=inputs_embeds, attention_mask=inputs.attention_mask, max_new_tokens=128, pad_token_id=tokenizer.eos_token_id)
    return tokenizer.decode(outputs[0], skip_special_tokens=True).strip()

def auto_caption(input_folder, process_images=True, process_videos=True, overwrite_captions=True, fps_divisor=16, max_frames=49):
    img_dir, vid_dir = os.path.join(input_folder, "img"), os.path.join(input_folder, "vid")
    output_img_dir, output_vid_dir = os.path.join(input_folder, "input/images"), os.path.join(input_folder, "input/videos")
    os.makedirs(output_img_dir, exist_ok=True)
    os.makedirs(output_vid_dir, exist_ok=True)

    if process_images:
        for img_file in os.listdir(img_dir):
            if img_file.lower().endswith((".png", ".jpg", ".jpeg")):
                input_img = os.path.join(img_dir, img_file)
                output_img = os.path.join(output_img_dir, img_file)
                resize_image(input_img, output_img)
                caption_path = os.path.join(output_img_dir, f"{os.path.splitext(img_file)[0]}.txt")
                if overwrite_captions or not os.path.exists(caption_path):
                    caption = generate_caption([output_img])
                    with open(caption_path, "w") as f:
                        f.write(caption)

    if process_videos:
        for vid_file in os.listdir(vid_dir):
            if vid_file.lower().endswith((".mp4", ".mov", ".avi", ".mkv")):
                input_vid = os.path.join(vid_dir, vid_file)
                output_vid = os.path.join(output_vid_dir, vid_file)
                frames_folder = os.path.join(output_vid_dir, f"{os.path.splitext(vid_file)[0]}_frames")
                resize_and_extract_video_frames(input_vid, output_vid, frames_folder, fps_divisor=fps_divisor, max_frames=max_frames)
                frame_paths = sorted([os.path.join(frames_folder, f) for f in os.listdir(frames_folder) if f.endswith(".jpg")])
                caption_path = os.path.join(output_vid_dir, f"{os.path.splitext(vid_file)[0]}.txt")
                if overwrite_captions or not os.path.exists(caption_path):
                    caption = generate_caption(frame_paths)
                    with open(caption_path, "w") as f:
                        f.write(caption)
