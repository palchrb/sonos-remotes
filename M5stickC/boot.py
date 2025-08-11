import M5
import time

# Initialiser enheten og skjermen
M5.begin()
try:
    M5.Lcd.setRotation(3)  # 270° rotasjon, juster etter behov
except Exception as e:
    print("Feil ved setRotation:", e)

# Fyll skjermen med svart bakgrunn
M5.Lcd.fillScreen(0x000000)

# Sett en større tekststørrelse for splash-skjermen
M5.Lcd.setTextSize(3)

# Plasser teksten midt på skjermen (juster x og y etter skjermens oppløsning)
M5.Lcd.setCursor(20, 60)
M5.Lcd.setTextColor(0xFFFFFF)
M5.Lcd.print("Starter...")

# La splash-skjermen vises i 2 sekunder
time.sleep(2)

# Fortsett oppstarten (f.eks. importer og kall main)
import main
