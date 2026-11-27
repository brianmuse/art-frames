# Using Art Frames

This page explains the main pages of the Art Frames web app. To install Art Frames, see the [README](../README.md#install).

## The sidebar

- **Home:** what each TV shows now.
- The TV selector names the TV that the next pages act on:
  - **Now:** what the TV shows, and why.
  - **Schedule:** which layout shows when, and the turn-on time.
  - **Picture:** the look, mats, brightness and screen tests.
  - **Sensor:** the optional room sensor.
  - **Health:** problems to fix on this TV.
  - **TV settings:** the name, the address and pairing.
- Household pages apply to every TV:
  - **Layouts:** see the installed layouts and change their settings.
  - **Artwork:** your artwork and the **Find art** tab.
  - **Household settings:** the timezone, the refresh speed and the daily artwork.
  - **Server:** how you reach Art Frames, your password, updates and backups.

## Layouts

Each layout has its own page with the fields that the layout declares. After you save a secret field, Art Frames shows it as **Saved** and never shows the value again. Use **Replace** to change it, or **Remove** to clear it.

## Artwork

- **Your artwork** lists your pieces. Use **Upload artwork** to add an image.
- **Find art** imports public-domain images from museums.
- Turn on **Include in daily artwork** for each piece that Art Frames can pick.
- Open **Framing** to set the crop, the zoom and the **Mat**. A mat is a border of up to 10% of the screen height, in any color.

Changes save automatically after a short pause. Art Frames keeps the original upload.

With a mat on, website content stays inside the painting opening. To turn mats off on one TV, open **Picture** → **Mats** → **No mats on this TV**.

## Now

The **Now** page (`/tv/<id>`) shows the picture that is **On the TV** and a panel with the layout, the reason, the next change and the turn-on time.

- **Show now** shows a layout at once. Choose how long the schedule waits.
- **More actions** → **Check TV** asks the TV what it shows. It never wakes the TV.
- **More actions** → **Turn on now** wakes the TV. It shows only when the TV has a MAC address.
- **More actions** → **Preview first…** opens a preview. Choose a layout and select **Make a preview**. Then select **Show on** *TV name* to send that image. The schedule holds it until the next change.

A send succeeds only after the TV reports the new artwork as selected. If it does not, Art Frames says so and keeps the previous upload on the TV. If someone changed the picture on the TV, the page shows what the TV showed at the last check.

## Picture

The **Picture** page (`/tv/<id>/picture`) controls how the TV looks in Art Mode. The TV must be on in Art Mode. These controls never wake the TV.

- **Read from TV** reads the current settings.
- **Look** sets the color temperature, the brightness, the TV's light sensor and the mats in one step. Select **Apply**.
- **Brightness** sets one level. With a room sensor on, the sensor sets it instead.

Under **Advanced**:

- **More picture settings** shows the **TV light sensor**, **Sleep After**, **Motion sensitivity** and every Art Mode setting that the TV reports.
- **TV details** shows the model, the firmware and the Art Mode API.
- **Tests** has controls for testing a TV only:
  - **Screen power:** **Try the power-off key**, **Read TV power** and **Turn screen on**.
  - **Soft blank:** **Blank the screen** shows a black image at the lowest brightness. **Undo blank** puts the picture and brightness back. While the screen is blank, Art Frames sends no artwork.
  - **Standby image test:** checks if the TV takes a new image while its screen is off.

## Activity and the command queue

Buttons that contact a TV do not wait on the page. Art Frames adds the command to that TV's queue and shows "Queued" at once.

- Each TV runs one command at a time, in order. A command waits for a running scheduled update to finish.
- If you press the same button again while its command is last in the queue, Art Frames does not add it twice.
- A TV holds up to 10 waiting commands.
- A result where the TV did nothing, for example because it is asleep, shows as "Not done".

Open **Activity** in the top bar to see what runs now and what waits. You can **Cancel** a waiting command. Activity also shows the last 100 results. The queue lives in memory, so a restart clears it.

## More than one TV

Use the TV selector in the sidebar to switch TVs, or **Add TV** to add another Frame. One server supports up to eight TVs.

- Each TV has its own pairing, schedule, previews and history.
- All TVs share the artwork and the layout settings.
- Each TV gets a different painting each day when you have enough artwork.

## Turn on at a set time

Art Frames can turn a TV on at a set time. Set the **Turn-on time** on the **Schedule** page.

Set up each TV first:

1. On the TV, turn on **Power On with Mobile** and **IP Remote**.
2. In **TV settings** → **Address**, save the TV's MAC address. **Find TVs** can fill it in.

Art Frames sends Wake-on-LAN, then waits for Art Mode before it uploads. Normal refreshes never wake a sleeping TV. If the TV does not confirm the wake, Art Frames tries again for a short time.

In the ten minutes before the turn-on time, Art Frames prepares the picture with that time as its clock. So a layout that starts at 07:00 is ready for a 07:00 turn-on. The previous painting can show for a short time while the TV starts.

See [Samsung's IP-control guide](https://image-us.samsung.com/SamsungUS/samsungbusiness/tv-ci-resources/Samsung-IP-Control.pdf).

## Settings and the room sensor

For the layout schedule, the room sensor and TV address recovery, see [Configuration](configuration.md).
