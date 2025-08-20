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

# Aktiver CORS for hele applikasjonen
from flask_cors import CORS
CORS(app)

# --------------------------
# AUTH: Én global secret + LAN/Tailscale bypass
# --------------------------
import ipaddress
from functools import wraps

# Sett hemmeligheten her eller via env (ANBEFALT: SOCORFID_SECRET)
SECRET = "secret string!"

# Nett som slipper auth (LAN/Tailscale m.m.)
TRUSTED_CIDRS = [
    "127.0.0.0/8", "10.0.0.0/8", "192.168.0.0/16",
    "169.254.0.0/16", "fc00::/7", "fe80::/10", "::1/128",
    "100.64.0.0/10",  # Tailscale CGNAT
]
TRUSTED_NETWORKS = [ipaddress.ip_network(c) for c in TRUSTED_CIDRS]

def _client_ip():
    return request.remote_addr or "0.0.0.0"

def _is_trusted_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in TRUSTED_NETWORKS)
    except Exception:
        return False

def _extract_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None

def _authorized():
    # LAN/Tailscale bypass
    if _is_trusted_ip(_client_ip()):
        return True
    # Ellers: må matche SECRET
    tok = _extract_bearer()
    return bool(tok and SECRET and tok == SECRET)

def require_auth_or_local(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _authorized():
            return jsonify({
                "error": "Unauthorized",
                "hint": "LAN/Tailscale allowed without auth; otherwise use Authorization: Bearer <secret>"
            }), 401
        return fn(*args, **kwargs)
    return wrapper
# --------------------------


# Katalogen der lokale podcast XML-filer ligger
PODCAST_FEED_DIR = "/home/palchrb/NRK_P/nrk-pod-feeds/docs/rss"

# Fil for lagring av mapping (device_id --> høyttaler-IP)
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
# LAST RFID-ENDPOINT
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
# HENTING AV HØYTTALERE & VALG
# --------------------------
def discover_speakers():
    found = discover()
    speakers = {}
    if found:
        for device in found:
            # Hvis enheten er del av en gruppe, bruk koordinatorens navn og IP
            coordinator = device.group.coordinator
            if coordinator:
                speakers[coordinator.player_name] = coordinator.ip_address
            else:
                speakers[device.player_name] = device.ip_address
    return speakers


@app.route("/speakers", methods=["GET"])
@require_auth_or_local
def get_speakers_endpoint():
    speakers = discover_speakers()
    return jsonify(speakers)

@app.route("/set_speaker", methods=["POST"])
@require_auth_or_local
def set_speaker_endpoint():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400

    speakers = discover_speakers()
    if "speaker" in data:
        name = data["speaker"]
        if name not in speakers:
            return jsonify({"error": "Ukjent høyttaler"}), 400
        chosen_ip = speakers[name]
    elif "ip" in data:
        chosen_ip = data["ip"]
    else:
        return jsonify({"error": "Mangler speaker/ip"}), 400

    set_speaker_for_device(device_id, chosen_ip)
    return jsonify({"status": "Høyttaler oppdatert", "device_id": device_id, "ip": chosen_ip})

# Funksjoner for interne kall (brukes i play_by_card)
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
# AVSPILLING VIA SPOTIFY PLAYLINK
# --------------------------
@app.route("/play/playlink", methods=["POST"])
@require_auth_or_local
def play_playlink():
    data = request.json
    device_id = data.get("device_id")
    media = data.get("media")
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400
    if not media:
        return jsonify({"error": "media (Spotify-playlink) mangler"}), 400

    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400

    try:
        sonos = SoCo(speaker_ip)
        sonos.stop()
        try:
            sonos.avTransport.EndDirectControlSession([("InstanceID", 0)])
        except Exception as e:
            print("Ingen direkte kontrollsession å avslutte, fortsetter...", e)
        sonos.clear_queue()
        plugin = ShareLinkPlugin(sonos)
        queue_position = plugin.add_share_link_to_queue(media)
        print("Element lagt til køen på posisjon:", queue_position)
        sonos.play_from_queue(0, start=True)
        return jsonify({"status": "Avspilling startet via PlayLink", "position": queue_position})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# HJELPEFUNKSJONER FOR NRK-PROGRAM
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
        raise ValueError("Ugyldig NRK-serie-URL. Forventet format: .../serie/<series-navn>/<programkode>")
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
        raise ValueError(f"Kunne ikke hente NRK metadata for {program_id}: HTTP {response.status_code}")
    return response.json()

def build_didl_metadata(sonos_uri, metadata_api):
    title = metadata_api.get("preplay", {}).get("titles", {}).get("subtitle", "Ukjent tittel")
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

def play_nrk_series(nrk_url, speaker_ip):
    episodes = []
    series_name = nrk_url.rstrip('/').split('/')[-2]
    current_url = nrk_url

    while True:
        current_program_id = get_program_id(current_url)
        print(f"Henter data for program: {current_program_id}")
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
            print("Ingen flere 'next'-episoder funnet.")
            break
        
        next_program_id = next_href.split("/")[-1]
        current_url = f"https://radio.nrk.no/serie/{series_name}/{next_program_id}"
        print(f"Neste episode: {next_program_id}")

    sonos = SoCo(speaker_ip)
    sonos.stop()
    try:
        sonos.avTransport.EndDirectControlSession([("InstanceID", 0)])
    except Exception as e:
        print("Ingen direkte kontrollsession å avslutte, fortsetter...", e)
    sonos.clear_queue()
    print("Køen ble tømt.")

    for idx, (uri, metadata) in enumerate(episodes):
        print(f"Legger til episode {idx+1}: {uri}")
        sonos.avTransport.AddURIToQueue([
            ("InstanceID", 0),
            ("EnqueuedURI", uri),
            ("EnqueuedURIMetaData", metadata),
            ("DesiredFirstTrackNumberEnqueued", 0),
            ("EnqueueAsNext", 0),
        ])
    
    sonos.play_from_queue(0, start=True)
    return {"status": "Avspilling startet fra NRK program", "antall_episoder": len(episodes)}

@app.route("/play/nrk_program", methods=["POST"])
@require_auth_or_local
def play_nrk_program():
    data = request.json
    device_id = data.get("device_id")
    media = data.get("media")  # For NRK-program, media tolkes som NRK-URL
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400
    if not media:
        return jsonify({"error": "media (NRK-URL) mangler i request"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400
    try:
        result = play_nrk_series(media, speaker_ip)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# NRK PODCAST: serie (.xml) ELLER enkel episode (episode-URL)
# --------------------------
import html
import unicodedata

def _norm(s):
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_episode_title(episode_page_url, episode_id=None):
    """Hent episodetittel fra NRK-episode-siden."""
    resp = requests.get(episode_page_url, timeout=10)
    resp.raise_for_status()
    html_text = resp.text

    if not episode_id:
        episode_id = episode_page_url.rstrip("/").split("/")[-1]

    m = re.search(
        r'"episodeId"\s*:\s*"' + re.escape(episode_id) + r'".*?"titles"\s*:\s*\{\s*"title"\s*:\s*"([^"]+)"',
        html_text,
        re.DOTALL,
    )
    if m:
        return _norm(html.unescape(m.group(1)))

    m2 = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
    if m2:
        return _norm(html.unescape(m2.group(1)))
    m3 = re.search(r"<title>([^<]+)</title>", html_text)
    if m3:
        return _norm(html.unescape(m3.group(1)))
    raise ValueError("Kunne ikke finne episodetittel i NRK-siden.")

def find_enclosure_by_title(xml_path, wanted_title):
    """Returner (mp3_url, meta) for item der <title> matcher wanted_title."""
    with open(xml_path, "rb") as f:
        xml_content = f.read()
    root = ET.fromstring(xml_content)
    items = root.findall("./channel/item")

    wt = _norm(wanted_title)
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}

    for item in items:
        title_el = item.find("title")
        title = _norm(title_el.text if title_el is not None else "")
        if title != wt:
            continue
        enclosure = item.find("enclosure")
        if enclosure is None or "url" not in enclosure.attrib:
            continue
        mp3 = enclosure.attrib["url"]
        duration_el = item.find("itunes:duration", ns)
        duration = duration_el.text if duration_el is not None else "0:00:00"
        image_el = item.find("itunes:image", ns)
        album_art = image_el.attrib.get("href") if image_el is not None else ""
        meta = {"title": title, "duration": duration, "album_art": album_art}
        return mp3, meta

    raise ValueError("Episoden ble ikke funnet i XML.")

@app.route("/play/nrk_podcast", methods=["POST"])
@require_auth_or_local
def play_nrk_podcast():
    data = request.json
    device_id = data.get("device_id")
    media = data.get("media")  # Kan være "<slug>.xml" ELLER full episode-URL
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400
    if not media:
        return jsonify({"error": "media (XML-filnavn ELLER episode-URL) mangler i request"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400
    
    try:
        # Detekter episode-URL
        m_ep = re.match(r'^https?://radio\.nrk\.no/podkast/([a-z0-9_]+)/([A-Za-z0-9_-]+)$', media, re.IGNORECASE)

        sonos = SoCo(speaker_ip)
        sonos.stop()
        try:
            sonos.avTransport.EndDirectControlSession([("InstanceID", 0)])
        except Exception:
            pass
        sonos.clear_queue()

        if m_ep:
            # Enkel episode
            slug = m_ep.group(1)
            episode_id = m_ep.group(2)

            # 1) Finn tittel fra NRK-episode-siden
            title = extract_episode_title(media, episode_id=episode_id)

            # 2) Slå opp mp3 i lokal XML
            xml_file = os.path.join(PODCAST_FEED_DIR, f"{slug}.xml")
            mp3_url, meta = find_enclosure_by_title(xml_file, title)

            # 3) Legg kun denne i kø
            metadata = (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="-1" parentID="-1" restricted="true">'
                f'<dc:title>{saxutils.escape(meta["title"])}</dc:title>'
                '<upnp:class>object.item.audioItem</upnp:class>'
                f'<upnp:albumArtURI>{saxutils.escape(meta["album_art"])}</upnp:albumArtURI>'
                f'<res protocolInfo="sonos.com-http:*:audio/mpeg:*" duration="{saxutils.escape(meta["duration"])}">{saxutils.escape(mp3_url)}</res>'
                '</item>'
                '</DIDL-Lite>'
            )
            sonos.avTransport.AddURIToQueue([
                ("InstanceID", 0),
                ("EnqueuedURI", mp3_url),
                ("EnqueuedURIMetaData", metadata),
                ("DesiredFirstTrackNumberEnqueued", 0),
                ("EnqueueAsNext", 0),
            ])
            sonos.play_from_queue(0, start=True)
            return jsonify({"status": "NRK episode-avspilling startet", "episode_title": meta["title"], "mp3": mp3_url})

        # Ellers: hele feeden fra XML-fil (eksisterende oppførsel)
        full_path = os.path.join(PODCAST_FEED_DIR, media)
        with open(full_path, "rb") as f:
            xml_content = f.read()
        
        root = ET.fromstring(xml_content)
        items = root.findall("./channel/item")
        if not items:
            return jsonify({"error": "Ingen episoder funnet i feeden"}), 500

        episodes = []
        ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
        for item in items:
            title_el = item.find("title")
            title = title_el.text if title_el is not None else "Ukjent tittel"
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
            return jsonify({"error": "Ingen gyldige episoder funnet i feeden"}), 500
        
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
        return jsonify({"status": "NRK podcast-avspilling startet", "antall_episoder": len(episodes)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/queue", methods=["GET"])
@require_auth_or_local
def get_queue():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400
    try:
        sonos = SoCo(speaker_ip)
        queue = sonos.get_queue()
        result = []
        if not queue:
            return jsonify({"queue": []})
        for item in queue:
            title = getattr(item, "title", "Ukjent")
            uri = ""
            if hasattr(item, "resources") and item.resources:
                uri = item.resources[0].uri
            result.append({"title": title, "uri": uri})
        return jsonify({"queue": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/play_by_card", methods=["POST"])
@require_auth_or_local
def play_by_card():
    data = request.json
    device_id = data.get("device_id")
    card_id = data.get("card_id")
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400
    if not card_id:
        return jsonify({"error": "card_id mangler"}), 400

    with open("rfid_mappings.json", "r") as f:
        mapping_data = json.load(f)

    if card_id not in mapping_data:
        with open("last_unmapped_rfid.txt", "w") as f:
            f.write(card_id)
        return jsonify({"error": "RFID ikke funnet, lagret som siste udefinerte RFID"}), 404

    mapping = mapping_data[card_id]
    mapping_type = mapping.get("type")
    # Vi forventer at mapping inneholder nøkkelen "media" for alle typer
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
        return jsonify({"error": "Ukjent mapping-type"}), 400

@app.route("/add_mapping", methods=["POST"])
@require_auth_or_local
def add_mapping():
    data = request.json
    card_id = data.get("card_id")
    mapping_type = data.get("type")
    media = data.get("media")
    if not card_id or not mapping_type or not media:
        return jsonify({"error": "Følgende felt må være med: card_id, type og media"}), 400

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
        
        # Hvis lagringen av mapping var vellykket, tøm last_unmapped_rfid.txt
        try:
            with open("last_unmapped_rfid.txt", "r+") as f:
                current = f.read().strip()
                # Slett bare hvis innholdet stemmer overens med card_id vi nettopp lagret
                if current == card_id:
                    f.seek(0)
                    f.truncate()
        except Exception as e:
            print("Feil ved tømming av last_unmapped_rfid.txt:", e)
        
        return jsonify({"status": "Mapping lagt til", "card_id": card_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/next", methods=["POST"])
@require_auth_or_local
def next_track():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400
    try:
        sonos = SoCo(speaker_ip)
        sonos.next()
        return jsonify({"status": "Next track command sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/status")
#@require_auth_or_local
def status():
    return "OK", 200

@app.route("/play_pause", methods=["POST"])
@require_auth_or_local
def play_pause():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400

    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400

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
@require_auth_or_local
def previous_track():
    data = request.json
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id mangler"}), 400

    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400

    try:
        sonos = SoCo(speaker_ip)
        sonos.previous()
        return jsonify({"status": "Previous track command sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/mappings", methods=["GET"])
@require_auth_or_local
def get_mappings():
    with open("rfid_mappings.json") as f:
        data = json.load(f)
    return jsonify(data)


# --------------------------
# SONOS: UNGROUP (splitter alle grupper)
# --------------------------
@app.route("/ungroup", methods=["POST"])
@require_auth_or_local
def ungroup_all():
    try:
        zones = discover(timeout=3) or set()
        ungrouped = []
        already_solo = []
        errors = []

        for z in zones:
            try:
                grp = z.group
                if grp and len(grp.members) > 1:
                    z.unjoin()
                    ungrouped.append(z.player_name)
                else:
                    already_solo.append(z.player_name)
            except Exception as e:
                errors.append({"player": getattr(z, "player_name", "ukjent"), "error": str(e)})

        return jsonify({
            "found": len(zones),
            "ungrouped": sorted(ungrouped),
            "already_solo": sorted(already_solo),
            "errors": errors
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------
# SONOS: GROUP (grupper navngitte høyttalere)
# Body (JSON):
# {
#   "speakers": ["Edith","Sverre"],   # påkrevd, minst 2. Støtter også unik prefiks ("ed" -> "Edith")
#   "coordinator": "Edith",           # valgfri, default = første i speakers-lista
#   "exact": true,                    # valgfri: fjern alle andre fra gruppa til koordinator
#   "device_id": "m5stick-123"        # valgfri: mapper denne til koordinator-IP via set_speaker_for_device()
# }
# --------------------------
@app.route("/group", methods=["POST"])
@require_auth_or_local
def group_speakers():
    data = request.json or {}
    names = data.get("speakers") or []
    if isinstance(names, str):
        # Støtt "Edith Sverre" eller "Edith,Sverre"
        if "," in names:
            names = [n.strip() for n in names.split(",") if n.strip()]
        else:
            names = [n for n in names.split() if n]

    if len(names) < 2:
        return jsonify({"error": "Oppgi minst to høyttalere i 'speakers'"}), 400

    coordinator_name = data.get("coordinator") or names[0]
    exact = bool(data.get("exact", False))
    device_id = data.get("device_id")

    zones = discover(timeout=3) or set()
    if not zones:
        return jsonify({"error": "Fant ingen Sonos-enheter"}), 500

    by_name = {z.player_name.lower(): z for z in zones}
    available = sorted(by_name.keys())

    # Hjelpefunksjon: eksakt eller unik prefiks-match
    def resolve(raw):
        key = raw.lower()
        if key in by_name:
            return by_name[key]
        matches = [v for n, v in by_name.items() if n.startswith(key)]
        return matches[0] if len(matches) == 1 else None

    # Løs opp navnelista
    resolved = []
    missing = []
    for raw in names:
        z = resolve(raw)
        if z:
            resolved.append(z)
        else:
            missing.append(raw)

    if missing:
        return jsonify({
            "error": "Ukjente/ambigue navn i 'speakers'",
            "missing": missing,
            "available": available
        }), 400

    # Finn koordinator blant de resolverte
    coord = resolve(coordinator_name)
    if not coord or all(coord.uid != z.uid for z in resolved):
        return jsonify({"error": "Koordinator må være blant 'speakers' og være entydig"}), 400

    wanted_set = {z.uid for z in resolved}
    errors, added, already = [], [], []

    # Join alle ønskede inn i koordinators gruppe
    for z in resolved:
        if z.uid == coord.uid:
            continue
        try:
            if z.group and z.group.coordinator.uid == coord.uid:
                already.append(z.player_name)
            else:
                z.join(coord)
                added.append(z.player_name)
        except Exception as e:
            errors.append({"player": z.player_name, "error": str(e)})

    removed = []
    # exact=True: fjern alle andre som ligger i koordinators gruppe, men ikke står på lista
    try:
        grp = coord.group
        if exact and grp:
            for m in list(grp.members):
                if m.uid == coord.uid:
                    continue
                if m.uid not in wanted_set:
                    try:
                        m.unjoin()
                        removed.append(m.player_name)
                    except Exception as e:
                        errors.append({"player": m.player_name, "error": str(e)})
    except Exception as e:
        errors.append({"stage": "exact_prune", "error": str(e)})

    # Rapporter endelig gruppesammensetning + koordinatorinfo
    members_info = []
    try:
        grp = coord.group
        members = grp.members if grp else [coord]
        for m in members:
            members_info.append({"name": m.player_name, "ip": m.ip_address, "uid": m.uid})
    except Exception:
        pass

    # Valgfritt: mappe device_id -> koordinator-IP
    mapped_device_id = None
    if device_id:
        try:
            set_speaker_for_device(device_id, coord.ip_address)
            mapped_device_id = device_id
        except Exception as e:
            errors.append({"stage": "map_device", "error": str(e)})

    return jsonify({
        "coordinator": {
            "name": coord.player_name,
            "ip": coord.ip_address,
            "uid": coord.uid
        },
        "added": sorted(added),
        "already_in_group": sorted(already),
        "removed_due_to_exact": sorted(removed),
        "final_group": [m["name"] for m in members_info],
        "members": members_info,
        "mapped_device_id": mapped_device_id,
        "errors": errors
    })



@app.route("/play/stream", methods=["POST"])
@require_auth_or_local
def play_stream():
    data = request.json
    device_id = data.get("device_id")
    uri = data.get("uri")
    if not device_id or not uri:
        return jsonify({"error": "device_id/uri mangler"}), 400
    speaker_ip = get_speaker_for_device(device_id)
    if not speaker_ip:
        return jsonify({"error": "Ingen høyttaler valgt for denne device_id"}), 400
    try:
        sonos = SoCo(speaker_ip)
        sonos.stop()
        try:
            sonos.avTransport.EndDirectControlSession([("InstanceID", 0)])
        except Exception:
            pass
        sonos.clear_queue()
        sonos.play_uri(uri)
        return jsonify({"status": "Avspilling startet", "uri": uri})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# SONOS: STATUS FOR ALLE HØYTTALERE
# --------------------------
@app.route("/players/status", methods=["GET"])
@require_auth_or_local
def players_status():
    try:
        zones = discover(timeout=3) or set()
        players = []

        for z in zones:
            try:
                info = z.get_current_transport_info() or {}
                state = info.get("current_transport_state") or "UNKNOWN"

                track = None
                if state in ("PLAYING", "PAUSED_PLAYBACK"):
                    t = z.get_current_track_info() or {}
                    track = {
                        "title": t.get("title"),
                        "artist": t.get("artist"),
                        "album": t.get("album"),
                        "position": t.get("position"),
                        "uri": t.get("uri"),
                    }

                grp = z.group
                group_data = None
                is_coord = False
                if grp:
                    is_coord = grp.coordinator and grp.coordinator.uid == z.uid
                    group_data = {
                        "coordinator": grp.coordinator.player_name if grp.coordinator else None,
                        "members": [m.player_name for m in grp.members],
                    }

                players.append({
                    "name": z.player_name,
                    "ip": z.ip_address,
                    "state": state,                     # PLAYING / PAUSED_PLAYBACK / STOPPED / etc.
                    "volume": z.volume,
                    "muted": bool(z.mute),
                    "is_coordinator": bool(is_coord),
                    "group": group_data,
                    "track": track,
                })
            except Exception as e:
                players.append({
                    "name": getattr(z, "player_name", "ukjent"),
                    "ip": getattr(z, "ip_address", None),
                    "error": str(e),
                })

        players.sort(key=lambda p: p.get("name") or "")
        return jsonify({"found": len(zones), "players": players})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
