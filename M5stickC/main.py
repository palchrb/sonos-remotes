import network
import time
import machine
import json
import M5
from hardware import I2C, Pin
from unit import RFIDUnit
import requests2 as urequests

# wifi config
SSID = "xxxxx"
PASSWORD = "xxxxxx"
MY_IP = "xxxxxx"
MY_NETMASK = "255.255.255.0"
MY_GATEWAY = "XXXXX"
MY_DNS = "8.8.8.8"

SERVER_URL = "..." #app.py server address
DEVICE_ID = "M5Stick"  # Unik enhets-ID for denne M5Stick
INACTIVITY_TIMEOUT = 60000
last_activity = time.ticks_ms()
current_device_ip = None

# Global variabel for å unngå unødvendige displayoppdateringer
last_display = None

def update_inactivity():
    global last_activity
    last_activity = time.ticks_ms()

def check_inactivity():
    if time.ticks_diff(time.ticks_ms(), last_activity) > INACTIVITY_TIMEOUT:
        safe_update_display("Inaktiv 60 sek\nDeep sleep...")
        time.sleep(2)
        machine.deepsleep(INACTIVITY_TIMEOUT)

def fix_chars(s):
    return s.replace('Ø', 'O').replace('ø', 'o')

def get_battery_percentage():
    try:
        return M5.Power.getBatteryLevel()
    except Exception as e:
        print("Feil ved henting av batterinivå:", e)
        return 0

def setup_display():
    M5.begin()
    try:
        M5.Lcd.setRotation(3)
    except Exception as e:
        print("Feil ved setRotation, fortsetter:", e)
    M5.Lcd.fillScreen(0x000000)

def safe_update_display(text):
    global last_display
    if text != last_display:
        update_display(text)
        last_display = text

def update_display(text):
    # For GUI vises teksten modifisert med fix_chars (f.eks. "Kjokken" for "Kjøkken")
    text_for_display = fix_chars(text)
    M5.Lcd.fillScreen(0x000000)
    M5.Lcd.setTextSize(2)
    batt = get_battery_percentage()
    M5.Lcd.setCursor(200, 0)
    M5.Lcd.setTextColor(0x00FF00)
    M5.Lcd.setTextSize(1)
    M5.Lcd.print(str(batt) + "%")
    M5.Lcd.setTextSize(2)
    M5.Lcd.setCursor(10, 30)
    M5.Lcd.setTextColor(0xFFFFFF)
    M5.Lcd.print(text_for_display)
    print("Display:", text_for_display)

def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.ifconfig((MY_IP, MY_NETMASK, MY_GATEWAY, MY_DNS))
    wlan.connect(SSID, PASSWORD)
    timeout = 10
    while timeout > 0:
        check_inactivity()
        if wlan.isconnected():
            print("Koblet til WiFi:", wlan.ifconfig())
            safe_update_display("WiFi tilkoblet")
            update_inactivity()
            return True
        time.sleep(1)
        timeout -= 1
    safe_update_display("WiFi feilet!")
    print("WiFi-tilkobling feilet!")
    return False

def get_speakers():
    try:
        response = urequests.get(SERVER_URL + "/speakers")
        speakers = response.json()
        response.close()
        print("Hentet høyttalere:", speakers)
        return list(speakers.keys())
    except Exception as e:
        print("Feil ved henting av høyttalerliste:", e)
        return []

def draw_two_column_menu(options, selected, window_start, max_items_per_page):
    M5.Lcd.fillScreen(0x000000)
    M5.Lcd.setTextSize(2)
    M5.Lcd.setCursor(10, 5)
    M5.Lcd.setTextColor(0xFFFFFF)
    M5.Lcd.print("Velg hoyttaler:")
    batt = get_battery_percentage()
    M5.Lcd.setCursor(200, 0)
    M5.Lcd.setTextColor(0x00FF00)
    M5.Lcd.setTextSize(1)
    M5.Lcd.print(str(batt) + "%")
    M5.Lcd.setTextSize(2)
    rows_per_col = max_items_per_page // 2
    row_height = 25
    for idx in range(window_start, min(window_start + max_items_per_page, len(options))):
        offset = idx - window_start
        col = offset // rows_per_col
        row = offset % rows_per_col
        x = 10 if col == 0 else 110
        y = 30 + row * row_height
        if idx == selected:
            M5.Lcd.setTextColor(0xFFFF00)
        else:
            M5.Lcd.setTextColor(0xFFFFFF)
        M5.Lcd.setCursor(x, y)
        M5.Lcd.print(fix_chars(options[idx]))
    M5.update()

def show_speaker_menu(options):
    global last_activity
    selected = 0
    last_selected = -1
    max_items_per_page = 8
    while True:
        check_inactivity()
        M5.update()
        if selected != last_selected:
            page = selected // max_items_per_page
            window_start = page * max_items_per_page
            draw_two_column_menu(options, selected, window_start, max_items_per_page)
            last_selected = selected
        if M5.BtnB.wasPressed():
            update_inactivity()
            selected += 1
            if selected >= len(options):
                selected = 0
            time.sleep(0.3)
        elif M5.BtnC.wasPressed():
            update_inactivity()
            safe_update_display("Restarting...")
            time.sleep(1)
            machine.reset()
        elif M5.BtnA.wasPressed():
            update_inactivity()
            safe_update_display("Valgt: " + fix_chars(options[selected]))
            print("Valgt hoyttaler:", options[selected])
            time.sleep(2)
            return options[selected]
        time.sleep(0.1)

def set_speaker(speaker_name):
    try:
        data = {"speaker": speaker_name, "device_id": DEVICE_ID}
        payload = json.dumps(data)
        payload = payload.replace("ø", "\\u00f8").replace("Ø", "\\u00F8")
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        print("Setter hoyttaler for", DEVICE_ID, ":", speaker_name)
        response = urequests.post(SERVER_URL + "/set_speaker", data=payload.encode("utf-8"), headers=headers)
        result = response.json()
        response.close()
        update_inactivity()
        return result
    except Exception as e:
        print("Feil ved valg av hoyttaler:", e)
        return {}

def set_next():
    try:
        data = {"device_id": DEVICE_ID}
        payload = json.dumps(data)
        payload = payload.replace("ø", "\\u00f8").replace("Ø", "\\u00F8")
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        print("Payload for set_next:", payload)
        response = urequests.post(SERVER_URL + "/next", data=payload.encode("utf-8"), headers=headers)
        result = response.json()
        response.close()
        update_inactivity()
        return result
    except Exception as e:
        print("Feil ved next-kommando:", e)
        return {}

def scan_rfid():
    while True:
        check_inactivity()
        i2c0 = I2C(0, scl=Pin(33), sda=Pin(32), freq=100000)
        addrs = i2c0.scan()
        print("I2C-enheter funnet:", addrs)
        if 0x28 not in addrs:
            safe_update_display("RFID: 0x28 ikke funnet")
            time.sleep(2)
            continue
        rfid_0 = RFIDUnit(i2c0, 0x28)
        safe_update_display("Skann RFID-kort")
        retries = 0
        max_retries = 10
        while retries < max_retries:
            M5.update()
            check_inactivity()
            if M5.BtnA.wasPressed() or M5.BtnC.wasPressed():
                update_inactivity()
                return None
            try:
                if rfid_0.is_new_card_present():
                    card_id = rfid_0.read_card_uid()
                    if card_id:
                        card_id_str = "".join("{:02X}".format(b) for b in card_id)
                        safe_update_display("Kort: " + card_id_str)
                        print("RFID UID lest:", card_id_str)
                        update_inactivity()
                        return card_id_str
                    else:
                        print("Ingen UID, prøver igjen...")
                else:
                    print("Ingen kort, prøver igjen...")
            except Exception as ex:
                print("RFID feil:", ex)
            retries += 1
            time.sleep(0.5)
        safe_update_display("Skann RFID-kort")
        time.sleep(1)

def send_card(card_id):
    safe_update_display("Sender kort-ID...")
    try:
        data = {"card_id": card_id, "device_id": DEVICE_ID}
        response = urequests.post(SERVER_URL + "/play_by_card", json=data)
        result = response.json()
        response.close()
        if "error" in result:
            safe_update_display("RFID ukjent")
            print("Backend feilmelding:", result)
            time.sleep(3)
            return False
        else:
            status = result.get("status", "Feil")
            safe_update_display("Svar: " + status)
            print("Svar fra backend:", result)
            update_inactivity()
            time.sleep(2)  # Kort pause før playback_mode
            return True
    except Exception as e:
        safe_update_display("Send feilet")
        print("Feil ved sending:", e)
        time.sleep(10)
        return False

# Oppdatert playback_mode() med isPressed() og isHolding()
def playback_mode():
    global last_activity
    safe_update_display("Spiller - Trykk for neste sang")
    start_time = time.ticks_ms()
    last_update = time.ticks_ms()
    while True:
        M5.update()
        # Oppdater displayet med lav frekvens for å unngå blinking
        if time.ticks_diff(time.ticks_ms(), last_update) > 2000:
            safe_update_display("Spiller - Trykk for neste sang")
            last_update = time.ticks_ms()
        if M5.BtnA.isPressed():
            # Sjekk om knappen er i langtrykk (isHolding())
            if M5.BtnA.isHolding():
                safe_update_display("Går tilbake til RFID-scan")
                time.sleep(1)
                return "exit"
            else:
                safe_update_display("Neste sang...")
                set_next()
                update_inactivity()
                safe_update_display("Spiller - Trykk for neste sang")
        if time.ticks_diff(time.ticks_ms(), start_time) > INACTIVITY_TIMEOUT:
            safe_update_display("Inaktiv 60 sek\nDeep sleep...")
            time.sleep(2)
            machine.deepsleep(INACTIVITY_TIMEOUT)
        time.sleep(0.1)

def main():
    global current_device_ip
    setup_display()
    if not connect_wifi():
        time.sleep(60)
        machine.deepsleep(INACTIVITY_TIMEOUT)
    speakers = get_speakers()
    if not speakers:
        safe_update_display("Ingen høyttalere")
        time.sleep(60)
        machine.deepsleep(INACTIVITY_TIMEOUT)
    safe_update_display("Hentet høyttalere")
    time.sleep(1)
    chosen = show_speaker_menu(speakers)
    safe_update_display("Valgt: " + fix_chars(chosen))
    result = set_speaker(chosen)
    if "ip" in result:
        current_device_ip = result["ip"]
    else:
        current_device_ip = None
    time.sleep(1)
    while True:
        card_id = scan_rfid()
        if card_id is None:
            chosen = show_speaker_menu(speakers)
            safe_update_display("Valgt: " + fix_chars(chosen))
            result = set_speaker(chosen)
            if "ip" in result:
                current_device_ip = result["ip"]
            else:
                current_device_ip = None
            time.sleep(1)
            continue
        if send_card(card_id):
            safe_update_display("Avspilling starter...")
            time.sleep(2)
            if current_device_ip:
                ret = playback_mode()
                if ret == "exit":
                    continue
            break
        else:
            safe_update_display("RFID ukjent\nPrøver igjen...")
            time.sleep(3)
    machine.deepsleep(INACTIVITY_TIMEOUT)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            from utility import print_error_msg
            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
