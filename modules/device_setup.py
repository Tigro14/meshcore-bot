"""Radio device setup, mixed into MeshCoreBot.

Syncing the radio clock, setting the device name, and the startup advert."""

import asyncio
from typing import Any

from meshcore import EventType


class DeviceSetupMixin:
    """Mixed into MeshCoreBot."""

    _record_send_failure: Any
    config: Any
    last_advert_time: Any
    last_clock_sync_time: Any
    logger: Any
    meshcore: Any

    async def set_device_name(self) -> bool:
        """Set device name to match bot_name from config if they differ.

        Checks the connected device's name and updates it to match the bot_name
        from config.ini if they differ. This ensures the device name matches the
        configured bot name before any adverts are sent.

        Returns:
            bool: True if check/update was successful (or not needed), False on error.
        """
        try:
            if not self.meshcore or not self.meshcore.is_connected:
                self.logger.warning("Cannot set device name - not connected to device")
                return False

            # Check if device name updates are enabled
            auto_update_name = self.config.getboolean('Bot', 'auto_update_device_name', fallback=True)
            if not auto_update_name:
                self.logger.debug("auto_update_device_name is disabled, skipping device name update")
                return True

            # Get desired name from config
            desired_name = self.config.get('Bot', 'bot_name', fallback=None)
            if not desired_name or desired_name.strip() == '':
                self.logger.debug("bot_name not set in config, skipping device name update")
                return True

            # Get current device name
            self.logger.info("Checking device name...")
            current_name = None

            try:
                if hasattr(self.meshcore, 'self_info') and self.meshcore.self_info:
                    self_info = self.meshcore.self_info
                    # Try to get name from self_info (could be dict or object)
                    if isinstance(self_info, dict):
                        current_name = self_info.get('name') or self_info.get('adv_name')
                    elif hasattr(self_info, 'name'):
                        current_name = self_info.name
                    elif hasattr(self_info, 'adv_name'):
                        current_name = self_info.adv_name
            except Exception as e:
                self.logger.debug(f"Could not get current device name: {e}")

            if current_name == desired_name:
                self.logger.info(f"Device name already matches config: '{desired_name}'")
                return True

            self.logger.info(f"Device name: '{current_name}', Config name: '{desired_name}'")
            self.logger.info("Updating device name to match config...")

            # Set the device name
            result = await self.meshcore.commands.set_name(desired_name)
            if result.type == EventType.OK:
                self.logger.info(f"✓ Device name updated to: '{desired_name}'")
                return True
            else:
                self.logger.warning(f"Failed to update device name: {result.payload if hasattr(result, 'payload') else result}")
                return False

        except (OSError, AttributeError, ValueError, KeyError) as e:
            self.logger.warning(f"Error checking/setting device name: {e}")
            return False

    async def send_startup_advert(self) -> None:
        """Send a startup advertisement if configured.

        Sends a 'bot online' status message to the mesh network. Can be configured
        as a local zero-hop broadcast or a flood message.
        """
        try:
            # Check if startup advert is enabled
            startup_advert = self.config.get('Bot', 'startup_advert', fallback='false').lower()
            if startup_advert == 'false':
                self.logger.debug("Startup advert disabled")
                return

            self.logger.info(f"Sending startup advert: {startup_advert}")

            # Add a small delay to ensure connection is fully established
            await asyncio.sleep(2)

            # Send the appropriate type of advert using meshcore.commands
            if startup_advert == 'zero-hop':
                self.logger.debug("Sending zero-hop advert")
                await asyncio.wait_for(
                    self.meshcore.commands.send_advert(flood=False),
                    timeout=30.0,
                )
            elif startup_advert == 'flood':
                self.logger.debug("Sending flood advert")
                await asyncio.wait_for(
                    self.meshcore.commands.send_advert(flood=True),
                    timeout=30.0,
                )
            else:
                self.logger.warning(f"Unknown startup_advert option: {startup_advert}")
                return

            # Update last advert time
            import time
            self.last_advert_time = time.time()

            self.logger.info(f"Startup {startup_advert} advert sent successfully")

        except (OSError, AttributeError, ValueError, RuntimeError, asyncio.TimeoutError) as e:
            if isinstance(e, asyncio.TimeoutError):
                # May trip the outage, which writes bot_metadata; keep that off the loop.
                await asyncio.to_thread(self._record_send_failure)
            self.logger.error(f"Error sending startup advert: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
