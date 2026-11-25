# Zero Life

A little man who lives in your M5Stack CardputerZero. He has an isometric studio
loft the size of the screen (320x170) and gets on with his life: fridge, TV on
the couch, bed in the loft, shower, fish, work, groceries. The accelerometer is
wired into his house, so tilting, shaking and flipping the device slosh the fish
tank, swing the lamp, slide things off shelves, break them, flicker the lights
and throw him around. He goes and looks at what you broke. You replace it.

It is always dusk or night outside: deep blue shadow, a warm lamp, fairy lights
on the loft, a glowing tank, and an old tree with a lantern and fireflies. The
house stands on a walled garden terrace with a fence, a vegetable bed, garden
lamps, a chimney and an owl on the wall. The clock on the brick wall shows the
real time.

Every few minutes he goes out to a friend's house, and the view follows him: a
dark modern house on three stepped tiers with glowing windows. Downstairs four
friends and he watch the match and cheer and drift to the bar for drinks; they
climb the stairs to the den for video games; a bedroom sits on top under a flat
roof. Tilting and shaking throws all five of them about. Then he walks home.

New in 0.4: a full day (morning, daylight, dusk, night) every twelve minutes, seasons by
the calendar (`--season winter` to try one), rain and snow, and sound (`--mute` to turn it
off). He has a dog, earns coins at the office and spends them at the shop and on replacing
what you break, and drives between places with the dog in the back. At home, coats and
towels swing, the fridge and front door fall open when the house leans, a bottle, an apple
and a pile of books roll about and knock into things, spills leave puddles he mops, and hard
shaking cracks the plaster until he patches it. A hard landing leaves him bandaged and
limping for a while; keep throwing him and he comes to the glass with a placard. One sharp
tap on the case is a knock. `--weather LAT,LON` shows the real weather for that place: it
asks api.open-meteo.com, so only use it if you are happy to send that location.

The lamp is a real light: each side of the furniture is lit by how squarely it
faces the bulb, things throw shadows away from it, and all of that swings when
the lamp swings. The man is a jointed figure 25 pixels tall. His arms and legs
swing when he walks, and when you throw him about they are loose chains that
trail behind him and flop.

One file, `zerolife.py`. Only dependency: `pygame`.

## Try it on a PC

    pip install pygame
    python zerolife.py

| Key | Does |
| --- | --- |
| Mouse, or arrows / WASD + Enter | move the cursor and click |
| Tab | jump to the next thing you can click |
| J / L | roll the house left / right (hold) |
| I / M | pitch it toward / away from you (hold) |
| U | flip it upside down (press again to put it back) |
| K | shake (hold) |
| T | type to him, then Enter (try: hello, sorry, food, park, shop, dance) |
| Esc | quit |

Click a dotted outline to replace the broken thing that lived there. Click the
tank to feed the fish and top up the water. Click him to poke him.

## Put it on the device (sideload)

Copy `dist/zerolife_0.4.1-1_all.deb` over and install it:

    scp dist/zerolife_0.4.1-1_all.deb <user>@<device-ip>:/tmp/
    ssh <user>@<device-ip> sudo apt install /tmp/zerolife_0.4.1-1_all.deb

`apt` pulls in `python3-pygame` if it is missing. The launcher rescans every few
seconds; "Zero Life" appears on the home screen. To run it from a shell instead:
`python3 zerolife.py --fb /dev/fb0`.

On the device the tilt keys are replaced by the real BMI270. The first time,
press **C** and follow the two prompts (lay it flat, then hold it upright) so the
game learns which way the sensor is mounted. Anything between "standing upright"
and "flat on the desk" counts as level, so he is not thrown around just because
you put the device down. Exit with Esc, or hold Home (the launcher sends SIGINT).

Useful flags: `--rotate 90|180|270` if the picture is sideways, `--kbd
/dev/input/eventN` to pick the keyboard, `--no-imu` to use the tilt keys,
`--i2c /dev/i2c-N` if the sensor is on another bus, `--fresh` to forget the save.

## Publish it to the CardputerZero store

The store is a GitHub pull-request pipeline against
[CardputerZero/packages](https://github.com/CardputerZero/packages).

1. Put this folder in a public GitHub repo.
2. Edit `app-builder.json`: `maintainer`, `store.source_repo`,
   `store.author.github`, `store.author.display_name`. Put your name in
   `LICENSE`. Leftover placeholder text is rejected automatically.
3. `python build_deb.py` writes `dist/zerolife_<version>-1_all.deb`. It warns
   until step 2 is done.
4. Submit, either way:
   - **Web:** sign in with GitHub at <https://dev.cardputer.cc>, drag the `.deb`
     in, fill the listing (icon `store/zerolife.png`, screenshots in
     `store/screenshots/`, tick the IMU permission). The portal opens the pull
     request for you.
   - **CLI:** `czdev publish` from
     [CardputerZero/AppBuilder](https://github.com/CardputerZero/AppBuilder),
     which reads `app-builder.json`.
5. CI checks the package (hash, `dpkg-deb`, ownership, version). A first release
   also gets a human review **with a demo on a real device**, so record a short
   video of it running.
6. For updates, raise `version` in `app-builder.json` and repeat. The package
   name `zerolife` belongs to whoever uploads it first.

## Tested on a real CardputerZero

Version 0.1.1 (then called Tiny Loft) was sideloaded onto a CardputerZero and started from the home
screen. The picture is the right way up, the keys move the cursor and click,
and the calibrated accelerometer tilts, sloshes and throws things as intended.
(0.1.0 froze when started from the launcher because it tried to open a desktop
window; an installed copy now always draws straight to the screen. If it ever
fails to start, the reason is written to `/tmp/zerolife.log` on the device.)

Still not checked:

- **Speed.** Furniture and the house are painted once and reused, and the
  light is only recomputed when something changes, so a frame costs about a
  quarter more than the first flat-coloured version did, measured on a PC. If
  the device cannot hold 30 fps the game drops the fireflies, embers, smoke
  and dust by itself; nobody has measured the real frame rate yet. Start-up
  takes about half a second on a PC and will take several seconds on the
  device, with a blank screen until the first frame.
- **pygame on the store.** The package declares `Depends: python3,
  python3-pygame`. Sideloading with `apt` resolved that. The docs do not say
  whether the on-device Store app does.
- **Category.** `"Games"` is a guess; the list of valid categories was not
  readable. The portal will show the real choices.

## Files

    zerolife.py           the game
    app-builder.json      store metadata, read by build_deb.py and czdev
    build_deb.py          builds the .deb (no dpkg needed, works on Windows)
    store/                icon (256x256) and screenshots (320x170)
    dist/                 the built package
