
import network
import time
import machine
import json
import M5
from M5 import Widgets, BtnA
from hardware import I2C, Pin
from unit import RFIDUnit
import requests2 as urequests
import ntptime

# --- KONFIGURASJON ---
SSID               = "..."
PASSWORD           = "..."
MY_IP              = "..."
MY_NETMASK         = "255.255.255.0"
MY_GATEWAY         = "....."
MY_DNS             = "8.8.8.8"
SERVER_URL         = "..." #app.py server
DEVICE_ID          = "AtomS3"
SPEAKER_NAME       = "....." #hardwired speaker selection


def connect_wifi():
    print("[WiFi] Starter tilkobling til", SSID)
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.ifconfig((MY_IP, MY_NETMASK, MY_GATEWAY, MY_DNS))
    wlan.connect(SSID, PASSWORD)
    for i in range(10):
        if wlan.isconnected():
            print(f"[WiFi] Tilkoblet ({i+1}s):", wlan.ifconfig())
            return True
        time.sleep(1)
        print(f"[WiFi] Venter ({i+1}/10)…")
    print("[WiFi] TILKOBLING FEILET")
    return False


def set_speaker():
    print("[SET] Setter speaker til", SPEAKER_NAME)
    data = {"speaker": SPEAKER_NAME, "device_id": DEVICE_ID}
    try:
        r = urequests.post(SERVER_URL + "/set_speaker", json=data)
        print("[SET] Svar:", r.json())
        r.close()
    except Exception as e:
        print("[SET] ERROR:", e)


def send_next():
    print("[NEXT] Sender NEXT-kommando")
    data = {"device_id": DEVICE_ID}
    try:
        r = urequests.post(SERVER_URL + "/next", json=data)
        print("[NEXT] Svar:", r.json())
        r.close()
    except Exception as e:
        print("[NEXT] ERROR:", e)


def scan_rfid_once(rdr):
    """
    Sjekk én gang om et kort er til stede.
    Returner card_id-streng eller None.
    """
    try:
        if rdr.is_new_card_present():
            uid = rdr.read_card_uid()
            if uid:
                card = "".join("{:02X}".format(b) for b in uid)
                print("[RFID] Funnet kort:", card)
                return card
    except Exception as e:
        print("[RFID] Feil under skanning:", e)
    return None


def send_card(card_id):
    print("[SEND] Sender kort-ID til backend:", card_id)
    data = {"card_id": card_id, "device_id": DEVICE_ID}
    try:
        r = urequests.post(SERVER_URL + "/play_by_card", json=data)
        print("[SEND] Svar:", r.json())
        r.close()
        return True
    except Exception as e:
        print("[SEND] ERROR:", e)
        return False


def check_night_mode():
    """
    Aktiv periode: 04:00–19:00 UTC.
    Sover utenfor dette intervallet ved å bruke deepSleep,
    og våkner automatisk etter angitt tid.
    """
    t = time.localtime()  # UTC etter ntptime.settime()
    now_secs = t[3] * 3600 + t[4] * 60 + t[5]
    sleep_start = 19 * 3600   # 19:00 UTC
    wake_time   =  4 * 3600   # 04:00 UTC

    if now_secs >= sleep_start or now_secs < wake_time:
        if now_secs < wake_time:
            wait = wake_time - now_secs
        else:
            wait = (24 * 3600 - now_secs) + wake_time

        hrs  = wait // 3600
        mins = (wait % 3600) // 60
        print(f"[SLEEP] Utenfor aktiv periode. Sover {hrs}t {mins}m til 04:00 UTC")
        # Deep sleep i mikrosekunder
        M5.Power.deepSleep(wait * 1000000, True)
        # Ved oppvåkning rebootes ESP32 og main() kjøres på nytt


def main():
    print("[BOOT] Starter main.py")
    M5.begin()
    Widgets.fillScreen(0x000000)
    label = Widgets.Label("Init…", 10, 10, 1.0, 0xFFFFFF)

    # WiFi + speaker
    if not connect_wifi():
        machine.reset()
    set_speaker()

    # NTP-synk (UTC)
    try:
        ntptime.settime()
        print("[NTP] Synkronisert, UTC tid:", time.localtime())
    except Exception as e:
        print("[NTP] Feil:", e)

    # Sjekk søvn-/våkne-skjema før drift
    check_night_mode()

    label.setText("Klar! BtnA=Next / RFID=Play")

    # Initialiser RFID-leser
    i2c = I2C(0, scl=Pin(1), sda=Pin(2), freq=100000)
    rdr = RFIDUnit(i2c, 0x28)

    # Hovedløkken
    while True:
        M5.update()

        # 1) Bytt sang ved knappetrykk
        if BtnA.wasPressed():
            print("[BTN] BtnA trykket → NEXT")
            send_next()
            time.sleep(0.3)  # debounce

        # 2) Sjekk RFID én gang
        card = scan_rfid_once(rdr)
        if card:
            if send_card(card):
                label.setText("Spiller: " + card)
                time.sleep(2)

        # 3) Sjekk om vi skal sove
        check_night_mode()

        # Kort pause for CPU
        time.sleep(0.1)

if __name__ == "__main__":
    main()
