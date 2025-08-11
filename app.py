from flask import Flask, request, jsonify
import json
import requests
import xml.sax.saxutils as saxutils
import re
import xml.etree.ElementTree as ET
import urllib.parse
import os
from soco import SoCo, discover
from soco.plugins.sharelink import ShareLinkPlugin
import threading

app = Flask(__name__)

# Enable CORS for the entire application
from flask_cors import CORS
CORS(app)


# Directory where local podcast XML files are located
PODCAST_FEED_DIR = "/home/palchrb/NRK_P/nrk-pod-feeds/docs/rss"

# File for storing mapping (device_id --> speaker IP)
DEVICE_MAPPING_FILE = "device_mapping.json"
mapping_lock = threading.Lock()

def load_mapping():
    try:
        with open(DEVICE_MAPPING_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_mapping(mapping):
    with open(DEVICE_MAPPING_FILE, "w") as f:
        json.dump(mapping, f)

def set_speaker_for_device(device_id, ip):
    with mapping_lock:
        mapping = load_mapping()
        mapping[device_id] = ip
        save_mapping(mapping)

def get_speaker_for_device(device_id):
    mapping = load_mapping()
    return mapping.get(device_id)

# --------------------------
# LAST RFID ENDPOINT
# --------------------------
@app.route("/last-rfid", methods=["GET"])
def last_rfid():
    try:
        with open("last_unmapped_rfid.txt", "r") as f:
            last = f.read().strip()
        return jsonify({"last_rfid": last})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# SPEAKER DISCOVERY & SELECTION
# --------------------------
def discover_speakers():
    found = discover()
    speakers = {}
    if found:
        for device in found:
            # If the device is part of a group, use the coordinator’s name and IP
            coordinator = device.group.coordinator
            if coordinator:
                speakers[coordinator.player_name] = coordinator.ip_address
            else:
                speakers[device.player_name] = device.ip_address
    return speakers


@app.route("/speakers", methods=["GET"])
def get_speakers_endpoint():
    speakers = discover_speakers()
    return jsonify(speakers)

@app.route("/set_speaker", methods=["POST"])
def set_speaker_endpoint():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400

    speakers = discover_speakers()
    if "speaker" in data:
        name = data["speaker"]
        if name not in speakers:
            return jsonify({"error": "Unknown speaker"}), 400
        chosen_ip = speakers[name]
    elif "ip" in data:
        chosen_ip = data["ip"]
    else:
        return jsonify({"error": "Missing speaker/ip"}), 400

    set_speaker_for_device(device_id, chosen_ip)
    return jsonify({"status": "Speaker updated", "device_id": device_id, "ip": chosen_ip})

# Functions for internal calls (used in play_by_card)
def play_playlink_internal(payload):
    with app.test_request_context(json=payload):
        return play_playlink()

def play_nrk_program_internal(payload):
    with app.test_request_context(json=payload):
        return play_nrk_program()

def play_nrk_podcast_internal(payload):
    with app.test_request_context(json=payload):
        return play_nrk_podcast()

# --------------------------
# PLAYBACK VIA SPOTIFY PLAYLINK
# --------------------------
@app.route("/play/playlink", methods=["POST"])
def play_playlink():
    data = request.json
    device_id = data.get("device_id")
    media = data.get("media")
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400
    if not media:
        return jsonify({"error": "media (Spotify playlink) missing"}), 400

    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "No speaker selected for this device_id"}), 400

    try:
        sonos = SoCo(speaker_ip)
        sonos.stop()
        try:
            sonos.avTransport.EndDirectControlSession([("InstanceID", 0)])
        except Exception as e:
            print("No direct control session to end, continuing...", e)
        sonos.clear_queue()
        plugin = ShareLinkPlugin(sonos)
        queue_position = plugin.add_share_link_to_queue(media)
        print("Item added to queue at position:", queue_position)
        sonos.play_from_queue(0, start=True)
        return jsonify({"status": "Playback started via PlayLink", "position": queue_position})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# HELPER FUNCTIONS FOR NRK PROGRAM
# --------------------------
def iso_duration_to_hms(iso_duration):
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(iso_duration)
    if not match:
        return "0:00:00"
    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    seconds = int(match.group(3)) if match.group(3) else 0
    return f"{hours}:{minutes:02d}:{seconds:02d}"

def get_program_id(nrk_url):
    parts = nrk_url.rstrip('/').split('/')
    if len(parts) < 5:
        raise ValueError("Invalid NRK series URL. Expected format: .../serie/<series-name>/<program-code>")
    return parts[-1]

def generate_sonos_uri(nrk_url, program_id=None):
    if not program_id:
        program_id = get_program_id(nrk_url)
    series_name = nrk_url.rstrip('/').split('/')[-2]
    sonos_uri = f"x-sonos-http:series%3a{urllib.parse.quote(series_name)}%3a1%3a{program_id}.unknown?sid=277&flags=0&sn=14"
    return sonos_uri

def fetch_nrk_metadata(program_id):
    api_url = f"https://psapi.nrk.no/playback/metadata/program/{program_id}"
    response = requests.get(api_url)
    if response.status_code != 200:
        raise ValueError(f"Could not fetch NRK metadata for {program_id}: HTTP {response.status_code}")
    return response.json()

def build_didl_metadata(sonos_uri, metadata_api):
    title = metadata_api.get("preplay", {}).get("titles", {}).get("subtitle", "Unknown title")
    iso_duration = metadata_api.get("duration", "PT0S")
    duration = iso_duration_to_hms(iso_duration)
    poster_images = metadata_api.get("preplay", {}).get("poster", {}).get("images", [])
    album_art = poster_images[-1]["url"] if poster_images else ""
    
    didl_metadata = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="-1" parentID="-1" restricted="true">'
        f'<dc:title>{saxutils.escape(title)}</dc:title>'
        '<upnp:class>object.item.audioItem.show</upnp:class>'
        f'<upnp:albumArtURI>{saxutils.escape(album_art)}</upnp:albumArtURI>'
        f'<res protocolInfo="sonos.com-http:*:audio/mpeg:*" duration="{saxutils.escape(duration)}">{saxutils.escape(sonos_uri)}</res>'
        '</item>'
        '</DIDL-Lite>'
    )
    return didl_metadata

# --------------------------
# PLAY NRK SERIES
# --------------------------
def play_nrk_series(nrk_url, speaker_ip):
    episodes = []
    series_name = nrk_url.rstrip('/').split('/')[-2]
    current_url = nrk_url

    while True:
        current_program_id = get_program_id(current_url)
        print(f"Fetching data for program: {current_program_id}")
        sonos_uri = generate_sonos_uri(current_url, current_program_id)
        metadata_api = fetch_nrk_metadata(current_program_id)
        didl_metadata = build_didl_metadata(sonos_uri, metadata_api)
        episodes.append((sonos_uri, didl_metadata))
        
        links = metadata_api.get("_links")
        next_href = None
        if links:
            next_dict = links.get("next")
            if next_dict:
                next_href = next_dict.get("href")
        
        if not next_href:
            print("No more 'next' episodes found.")
            break
        
        next_program_id = next_href.split("/")[-1]
        current_url = f"https://radio.nrk.no/serie/{series_name}/{next_program_id}"
        print(f"Next episode: {next_program_id}")

    sonos = SoCo(speaker_ip)
    sonos.stop()
    try:
        sonos.avTransport.EndDirectControlSession([("InstanceID", 0)])
    except Exception as e:
        print("No direct control session to end, continuing...", e)
    sonos.clear_queue()
    print("Queue cleared.")

    for idx, (uri, metadata) in enumerate(episodes):
        print(f"Adding episode {idx+1}: {uri}")
        sonos.avTransport.AddURIToQueue([
            ("InstanceID", 0),
            ("EnqueuedURI", uri),
            ("EnqueuedURIMetaData", metadata),
            ("DesiredFirstTrackNumberEnqueued", 0),
            ("EnqueueAsNext", 0),
        ])
    
    sonos.play_from_queue(0, start=True)
    return {"status": "Playback started from NRK program", "episode_count": len(episodes)}

@app.route("/play/nrk_program", methods=["POST"])
def play_nrk_program():
    data = request.json
    device_id = data.get("device_id")
    media = data.get("media")  # For NRK program, media is interpreted as an NRK URL
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400
    if not media:
        return jsonify({"error": "media (NRK URL) missing in request"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "No speaker selected for this device_id"}), 400
    try:
        result = play_nrk_series(media, speaker_ip)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# PLAYBACK OF NRK PODCAST (only via local XML files)
# --------------------------
@app.route("/play/nrk_podcast", methods=["POST"])
def play_nrk_podcast():
    data = request.json
    device_id = data.get("device_id")
    media = data.get("media")  # For podcasts, media is interpreted as a filename
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400
    if not media:
        return jsonify({"error": "media (XML filename) missing in request"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "No speaker selected for this device_id"}), 400
    
    try:
        full_path = os.path.join(PODCAST_FEED_DIR, media)
        with open(full_path, "rb") as f:
            xml_content = f.read()
        
        root = ET.fromstring(xml_content)
        items = root.findall("./channel/item")
        if not items:
            return jsonify({"error": "No episodes found in the feed"}), 500

        episodes = []
        ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
        for item in items:
            title_el = item.find("title")
            title = title_el.text if title_el is not None else "Unknown title"
            enclosure = item.find("enclosure")
            if enclosure is None or "url" not in enclosure.attrib:
                continue
            podcast_episode_url = enclosure.attrib["url"]
            duration_el = item.find("itunes:duration", ns)
            duration = duration_el.text if duration_el is not None else "0:00:00"
            image_el = item.find("itunes:image", ns)
            album_art = image_el.attrib.get("href") if image_el is not None else ""
            
            episodes.append({
                "title": title,
                "podcast_episode_url": podcast_episode_url,
                "duration": duration,
                "album_art": album_art
            })
        
        if not episodes:
            return jsonify({"error": "No valid episodes found in the feed"}), 500
        
        sonos = SoCo(speaker_ip)
        sonos.stop()
        try:
            sonos.avTransport.EndDirectControlSession([("InstanceID", 0)])
        except Exception as e:
            print("No direct control session to end, continuing...", e)
        sonos.clear_queue()
        
        for idx, ep in enumerate(episodes):
            metadata = (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="-1" parentID="-1" restricted="true">'
                '<res protocolInfo="sonos.com-http:*:audio/mpeg:*" duration="{duration}">{uri}</res>'
                '<dc:title>{title}</dc:title>'
                '<upnp:albumArtURI>{album_art}</upnp:albumArtURI>'
                '<upnp:class>object.item.audioItem.show</upnp:class>'
                '</item>'
                '</DIDL-Lite>'
            ).format(
                duration=saxutils.escape(ep["duration"]),
                uri=saxutils.escape(ep["podcast_episode_url"]),
                title=saxutils.escape(ep["title"]),
                album_art=saxutils.escape(ep["album_art"])
            )
            sonos.avTransport.AddURIToQueue([
                ("InstanceID", 0),
                ("EnqueuedURI", ep["podcast_episode_url"]),
                ("EnqueuedURIMetaData", metadata),
                ("DesiredFirstTrackNumberEnqueued", 0),
                ("EnqueueAsNext", 0),
            ])
        sonos.play_from_queue(0, start=True)
        return jsonify({"status": "NRK podcast playback started", "episode_count": len(episodes)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/queue", methods=["GET"])
def get_queue():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "No speaker selected for this device_id"}), 400
    try:
        sonos = SoCo(speaker_ip)
        queue = sonos.get_queue()
        result = []
        if not queue:
            return jsonify({"queue": []})
        for item in queue:
            title = getattr(item, "title", "Unknown")
            uri = ""
            if hasattr(item, "resources") and item.resources:
                uri = item.resources[0].uri
            result.append({"title": title, "uri": uri})
        return jsonify({"queue": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/play_by_card", methods=["POST"])
def play_by_card():
    data = request.json
    device_id = data.get("device_id")
    card_id = data.get("card_id")
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400
    if not card_id:
        return jsonify({"error": "card_id missing"}), 400

    with open("rfid_mappings.json", "r") as f:
        mapping_data = json.load(f)

    if card_id not in mapping_data:
        with open("last_unmapped_rfid.txt", "w") as f:
            f.write(card_id)
        return jsonify({"error": "RFID not found, stored as last unmapped RFID"}), 404

    mapping = mapping_data[card_id]
    mapping_type = mapping.get("type")
    # We expect mapping to contain the key "media" for all types
    if mapping_type == "program":
        payload = {"media": mapping.get("media"), "device_id": device_id}
        return play_nrk_program_internal(payload)
    elif mapping_type == "podcast":
        payload = {"media": mapping.get("media"), "device_id": device_id}
        return play_nrk_podcast_internal(payload)
    elif mapping_type == "playlink":
        payload = {"media": mapping.get("media"), "device_id": device_id}
        return play_playlink_internal(payload)
    else:
        return jsonify({"error": "Unknown mapping type"}), 400

@app.route("/add_mapping", methods=["POST"])
def add_mapping():
    data = request.json
    card_id = data.get("card_id")
    mapping_type = data.get("type")
    media = data.get("media")
    if not card_id or not mapping_type or not media:
        return jsonify({"error": "The following fields are required: card_id, type, and media"}), 400

    mapping_data = {"type": mapping_type, "media": media}
    try:
        try:
            with open("rfid_mappings.json", "r") as f:
                mappings = json.load(f)
        except FileNotFoundError:
            mappings = {}
        mappings[card_id] = mapping_data
        with open("rfid_mappings.json", "w") as f:
            json.dump(mappings, f, indent=4)
        
        # If saving the mapping was successful, clear last_unmapped_rfid.txt
        try:
            with open("last_unmapped_rfid.txt", "r+") as f:
                current = f.read().strip()
                # Only delete if the content matches the card_id we just saved
                if current == card_id:
                    f.seek(0)
                    f.truncate()
        except Exception as e:
            print("Error when clearing last_unmapped_rfid.txt:", e)
        
        return jsonify({"status": "Mapping added", "card_id": card_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/next", methods=["POST"])
def next_track():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "No speaker selected for this device_id"}), 400
    try:
        sonos = SoCo(speaker_ip)
        sonos.next()
        return jsonify({"status": "Next track command sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/status")
def status():
    return "OK", 200

@app.route("/play_pause", methods=["POST"])
def play_pause():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400

    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "No speaker selected for this device_id"}), 400

    try:
        sonos = SoCo(speaker_ip)
        state = sonos.get_current_transport_info().get('current_transport_state')
        if state == 'PLAYING':
            sonos.pause()
            action = 'paused'
        else:
            sonos.play()
            action = 'playing'
        return jsonify({"status": f"Toggled play/pause ({action})"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/previous", methods=["POST"])
def previous_track():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id missing"}), 400

    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "No speaker selected for this device_id"}), 400

    try:
        sonos = SoCo(speaker_ip)
        sonos.previous()
        return jsonify({"status": "Previous track command sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/mappings", methods=["GET"])
def get_mappings():
    with open("rfid_mappings.json") as f:
        data = json.load(f)
    return jsonify(data)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
