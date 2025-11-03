# sonos-remotes
(almost) screen-less rfid / button based Sonos remotes for my kids - This is also the backend i use for my sonos maubot plugin: https://github.com/palchrb/maubot_sonos

Worth noting that per now, what it can control is to start playing any spotify sharelink, as well as podcasts/programs from Norwegian broadcaster NRK (based on https://github.com/sindrel/nrk-pod-feeds) since this is what my kids listen to! 

App.py is the server, which uses Soco python library to control the sonos devices in my network.  

I have made 3 different devices; two rfid remotes based on M5Stack iot-devices: M5Stick C plus 2, and their rfid 2 reader, and an M5 atom S3 lite with their rfid 2 reader. FInal remote i made was an Ikea Styrbar zigbee based remote for my 4 year old.

What it controls is;
- For the m5 stick it maps all speakers and allow speaker selection on the simple screen
- Then you scan a RFID card (where the ID is mapped to a podcast or spotify sharelink), and voila
- Then you can press "next song"

Updated with bearer auth (and IP whitelist with no auth), as well as possibility to play specific episodes from NRK podcasts based on a sharelink, and not just the whole show)

Feel free to ask questions here: **[#sonosremotes:vibb.me](https://matrix.to/#/#sonosremotes:vibb.me)**          
       
