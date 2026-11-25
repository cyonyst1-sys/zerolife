# Tiny Loft

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

The lamp is a real light: each side of the furniture is lit by how squarely it
faces the bulb, things throw shadows away from it, and all of that swings when
the lamp swings. The man is a jointed figure 25 pixels tall. His arms and legs
swing when he walks, and when you throw him about they are loose chains that
trail behind him and flop.

One file, `tinyloft.py`. Only dependency: `pygame`.

## Try it on a PC

    pip install pygame
    python tinyloft.py

| Key | Does |
| --- | --- |
| Mouse, or arrows / WASD + Enter | move the cursor and click |
| Tab | jump to the next thing you can click |
| J / L | roll the house left / right (hold) |
| I / M | pitch it toward / away from you (hold) |
| U | flip it upside down (press again to put it back) |
| K | shake (hold) |
| Esc | quit |

Click a dotted outline to replace the broken thing that lived there. Click the
tank to feed the fish and top up the water. Click him to poke him.

## Put it on the device (sideload)

Copy `dist/tinyloft_0.1.2-1_all.deb` over and install it:

    scp dist/tinyloft_0.1.2-1_all.deb <user>@<device-ip>:/tmp/
    ssh <user>@<device-ip> sudo apt install /tmp/tinyloft_0.1.2-1_all.deb

`apt` pulls in `python3-pygame` if it is missing. The launcher rescans every few
seconds; "Tiny Loft" appears on the home screen. To run it from a shell instead:
`python3 tinyloft.py --fb /dev/fb0`.

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
3. `python build_deb.py` writes `dist/tinyloft_<version>-1_all.deb`. It warns
   until step 2 is done.
4. Submit, either way:
   - **Web:** sign in with GitHub at <https://dev.cardputer.cc>, drag the `.deb`
     in, fill the listing (icon `store/tinyloft.png`, screenshots in
     `store/screenshots/`, tick the IMU permission). The portal opens the pull
     request for you.
   - **CLI:** `czdev publish` from
     [CardputerZero/AppBuilder](https://github.com/CardputerZero/AppBuilder),
     which reads `app-builder.json`.
5. CI checks the package (hash, `dpkg-deb`, ownership, version). A first release
   also gets a human review **with a demo on a real device**, so record a short
   video of it running.
6. For updates, raise `version` in `app-builder.json` and repeat. The package
   name `tinyloft` belongs to whoever uploads it first.

## Tested on a real CardputerZero

Version 0.1.1 was sideloaded onto a CardputerZero and started from the home
screen. The picture is the right way up, the keys move the cursor and click,
and the calibrated accelerometer tilts, sloshes and throws things as intended.
(0.1.0 froze when started from the launcher because it tried to open a desktop
window; an installed copy now always draws straight to the screen. If it ever
fails to start, the reason is written to `/tmp/tinyloft.log` on the device.)

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

    tinyloft.py           the game
    app-builder.json      store metadata, read by build_deb.py and czdev
    build_deb.py          builds the .deb (no dpkg needed, works on Windows)
    store/                icon (256x256) and screenshots (320x170)
    dist/                 the built package
