# Repeater Management DM Commands

This document provides comprehensive documentation for all repeater management commands available via direct message (DM) to the bot.

**Note:** All repeater commands are DM-only and cannot be used in public channels.

**Command Format:** `!repeater <subcommand> [options]`

---

## Table of Contents

- [Repeater Discovery and Cataloging](#repeater-discovery-and-cataloging)
- [Listing and Viewing](#listing-and-viewing)
- [Location and Geolocation](#location-and-geolocation)
- [Purging Repeaters](#purging-repeaters)
- [Restoring Repeaters](#restoring-repeaters)
- [Statistics and Status](#statistics-and-status)
- [Contact List Management](#contact-list-management)
- [Auto-Purge Management](#repeater-auto-purge)
- [Testing and Debugging](#testing-and-debugging)

---

## Repeater Discovery and Cataloging

### `!repeater scan`

Scans the current device contacts and catalogs new repeaters into the database. Also updates location data for existing repeaters.

**Usage:**
```
!repeater scan
```

**What it does:**
- Scans all contacts on the device
- Identifies repeaters and RoomServers
- Catalogs new repeaters in the database
- Updates location data for existing repeaters

**Example Response:**
```
✅ Scanned contacts and cataloged 5 new repeaters
```

---

### `!repeater discover`

Discovers and adds companion contacts to the device.

**Usage:**
```
!repeater discover
```

**What it does:**
- Scans for companion contacts that should be added to the device
- Automatically adds discovered companions (if auto-add is enabled)

**Example Response:**
```
✅ Companion contact discovery initiated
```

---

## Listing and Viewing

### `!repeater list`

Lists repeater contacts stored in the database.

**Usage:**
```
!repeater list          # Show active repeaters only
!repeater list --all    # Show all repeaters (including purged)
!repeater list -a       # Short form for --all
```

**What it shows:**
- Repeater name
- Device type (Repeater or RoomServer)
- Last seen timestamp
- Purge count
- Active status (🟢 active, 🔴 purged)

**Example Response:**
```
📡 **Repeater Contacts** (Active):

🟢 📡 **Hillcrest**
   Type: Repeater
   Last seen: 2024-01-15 14:30
   Purge count: 0

🟢 🏠 **Northgate RoomServer**
   Type: RoomServer
   Last seen: 2024-01-15 12:00
   Purge count: 1
```

---

## Location and Geolocation

### `!repeater locations`

Shows location data statistics for all tracked repeaters.

**Usage:**
```
!repeater locations
```

**What it shows:**
- Total repeater count
- Percentage with GPS coordinates
- Percentage with city/state/country data
- Examples of repeater locations

**Example Response:**
```
📍 Repeater Locations (25 total):
GPS: 20 (80%)
City: 18 (72%)
State: 15 (60%)
Country: 15 (60%)
None: 5 (20%)
```

*(Second message with examples may follow)*

---

### `!repeater update-geo`

Updates missing geolocation data (city, state, country) for repeaters that have coordinates but are missing location information.

**Usage:**
```
!repeater update-geo                    # Update up to 10 repeaters (default)
!repeater update-geo 5                  # Update up to 5 repeaters
!repeater update-geo dry-run            # Preview what would be updated
!repeater update-geo dry-run 3           # Preview for up to 3 repeaters
```

**What it does:**
- Finds repeaters with GPS coordinates but missing city/state/country
- Performs reverse geocoding to fill in missing data
- Respects rate limits (2 seconds between requests)
- Skips repeaters with 0,0 coordinates (hidden locations)

**Example Response:**
```
🌍 Geolocation Update
Batch size: 10
Found: 8 repeaters with missing data
Updated: 6 repeaters
Errors: 0
Skipped: 2
✅ Geolocation data updated successfully!
```

---

### `!repeater geocode`

Manages geocoding operations for contacts with coordinates but missing location data.

**Usage:**
```
!repeater geocode              # Show geocoding status
!repeater geocode trigger       # Geocode 1 contact immediately
!repeater geocode bulk         # Bulk geocode up to 10 contacts (default)
!repeater geocode bulk 20      # Bulk geocode up to 20 contacts (max: 50)
!repeater geocode dry-run      # Preview what would be geocoded
!repeater geocode dry-run 5    # Preview for up to 5 contacts
!repeater geocode status       # Show detailed geocoding status
```

**Subcommands:**
- **trigger** - Manually trigger background geocoding for a single contact
- **bulk [N]** - Bulk geocode multiple contacts (default: 10, max: 50)
- **dry-run [N]** - Preview geocoding without making changes
- **status** - Show detailed geocoding status

**Example Response (status):**
```
🌍 Geocoding: 45/60 done, 15 pending
```

**Example Response (bulk):**
```
🌍 Bulk geocoding completed:
Found: 15 contacts
Updated: 12 contacts
Errors: 1
Skipped: 2
```

---

## Purging Repeaters

### `!repeater purge`

Removes repeaters from the device contact list.

**Usage:**
```
!repeater purge all                    # Purge all repeaters
!repeater purge all force               # Force purge all (uses multiple removal methods)
!repeater purge all "Clear all repeaters"           # With reason
!repeater purge 30                      # Purge repeaters older than 30 days
!repeater purge 14 "Auto-cleanup"                   # Purge with reason
!repeater purge "Hillcrest"             # Purge specific repeater by name
!repeater purge "Hillcrest" "Manual removal"      # With reason
```

**Options:**
- **all** - Remove all repeaters from device
- **all force** - Force removal using multiple methods (for stubborn contacts)
- **<days>** - Remove repeaters older than specified number of days
- **<name>** - Remove specific repeater (partial name match allowed)

**What it does:**
- Removes repeaters from the device contact list
- Updates database to mark repeaters as purged
- Records purge reason in database
- Verifies removal was successful

**Example Response:**
```
✅ Purged 12/12 repeaters
```

**If some fail:**
```
✅ Purged 10/12 repeaters
❌ Failed to purge 2 repeaters: Repeater1, Repeater2
💡 Try '!repeater purge all force' to force remove stubborn repeaters
```

---

### `!repeater auto-purge`

Manages automatic purging of repeaters when contact list approaches capacity limits.

**Usage:**
```
!repeater auto-purge                    # Show auto-purge status
!repeater auto-purge trigger            # Manually trigger auto-purge check
!repeater auto-purge enable             # Enable automatic purging
!repeater auto-purge disable            # Disable automatic purging
!repeater auto-purge monitor            # Run periodic contact monitoring
```

**Subcommands:**
- **trigger** - Manually trigger an auto-purge check and execution
- **enable** - Enable automatic purging
- **disable** - Disable automatic purging
- **monitor** - Run periodic contact monitoring (checks limits and triggers background geocoding)

**Example Response (status):**
```
🔄 Auto-Purge: ON | 285/300 (95%) | ⚠️ NEAR LIMIT
```

**Example Response (trigger):**
```
✅ Auto-purge triggered successfully
```

---

### `!repeater purge-status`

Shows detailed purge status and recommendations.

**Usage:**
```
!repeater purge-status
```

**What it shows:**
- Auto-purge enabled/disabled status
- Current contact count vs limit
- Usage percentage
- Health status (OK, NEAR LIMIT, FULL)

**Example Response:**
```
📊 Purge: ON | 285/300 (95%) | ⚠️ Near 280
```

---

## Restoring Repeaters

### `!repeater restore`

Restores a previously purged repeater (marks it as active in database, but does not re-add to device).

**Usage:**
```
!repeater restore "Hillcrest"                    # Restore by name
!repeater restore "Hillcrest" "Manual restore"   # With reason
```

**What it does:**
- Finds purged repeaters matching the name pattern
- Marks them as active in the database
- Records restore reason
- Does NOT automatically re-add to device contact list

**Example Response:**
```
✅ Restored repeater: Hillcrest
```

**If multiple matches:**
```
Multiple purged repeaters found matching 'Hill':
1. Hillcrest (Repeater)
2. Hilltop (RoomServer)

Please be more specific with the name.
```

---

## Statistics and Status

### `!repeater stats`

Shows comprehensive statistics about repeater tracking and management.

**Usage:**
```
!repeater stats
```

**What it shows:**
- Total contacts ever heard
- Currently tracked by device
- Recent activity (24 hours)
- Breakdown by MeshCore role (repeater, roomserver, companion, etc.)
- Breakdown by device type

**Example Response:**
```
📊 **Contact Tracking Statistics:**

• **Total Contacts Ever Heard:** 150
• **Currently Tracked by Device:** 45
• **Recent Activity (24h):** 12

**By MeshCore Role:**
• Repeater: 25
• RoomServer: 8
• Companion: 10
• Sensor: 2

**By Device Type:**
• Repeater: 25
• RoomServer: 8
• Companion: 10
```

---

### `!repeater status`

Shows contact list capacity status and limits.

**Usage:**
```
!repeater status
```

**What it shows:**
- Current contacts vs estimated limit
- Usage percentage
- Companion count
- Repeater count
- Stale contacts count
- Health status indicator

**Example Response:**
```
📊 285/300 (95%) | 👥10 📡25 ⏰5 | ⚠️ NEAR
```

**Status Indicators:**
- ✅ OK - Healthy usage
- ⚠️ NEAR - Approaching limit
- 🚨 FULL! - At or over limit

---

## Contact List Management

### `!repeater manage`

Automatically manages the contact list to prevent hitting capacity limits.

**Usage:**
```
!repeater manage                    # Perform automatic management
!repeater manage --dry-run          # Preview what would be done
!repeater manage -d                 # Short form for --dry-run
```

**What it does:**
- Analyzes contact list capacity
- Removes stale contacts
- Removes old repeaters (14+ days)
- If at limit, performs aggressive cleanup (7+ day repeaters, 14+ day stale contacts)
- Shows what actions were taken

**Example Response (dry-run):**
```
🔍 **Contact List Management (Dry Run)**

📊 Current status: 285/300 (95.0%)

⚠️ **Actions that would be taken:**
   • Remove 10 stale contacts
   • Remove old repeaters (14+ days)
```

**Example Response (executed):**
```
🔧 **Contact List Management Results**

📊 Final status: 270/300 (90.0%)

✅ **Actions taken:**
   • Removed 10 stale contacts
   • Removed 5 old repeaters (14+ days)
```

---

### `!repeater add`

Adds a discovered contact to the device contact list.

**Usage:**
```
!repeater add "ContactName"                    # Add by name
!repeater add "ContactName" <public_key>       # Add with public key
!repeater add "John" "0x1234..." "Manual add"   # With reason
```

**What it does:**
- Adds the specified contact to the device
- Can optionally specify public key
- Records addition reason

**Example Response:**
```
✅ Successfully added contact: John
```

---

### `!repeater auto`

Toggles manual contact addition setting.

**Usage:**
```
!repeater auto on                    # Enable auto-add
!repeater auto off                   # Disable auto-add
!repeater auto enable                # Enable (alternative)
!repeater auto disable               # Disable (alternative)
```

**What it does:**
- Controls whether contacts are automatically added when discovered
- When OFF: Bot requires manual addition via `!repeater add`
- When ON: Bot automatically adds discovered contacts

**Example Response:**
```
✅ Manual contact addition disabled
```

---

## Testing and Debugging

### `!repeater test`

Tests meshcore-cli command functionality.

**Usage:**
```
!repeater test
```

**What it does:**
- Tests if meshcore-cli commands are available
- Verifies help command works
- Verifies remove_contact command works
- Reports if purging functionality will work

**Example Response:**
```
🧪 **MeshCore-CLI Command Test Results**

📋 Help command: ✅ PASS
🗑️ Remove contact command: ✅ PASS

✅ All required commands are available.
```

**If commands are missing:**
```
🧪 **MeshCore-CLI Command Test Results**

📋 Help command: ✅ PASS
🗑️ Remove contact command: ❌ FAIL

⚠️ **WARNING**: remove_contact command not available!
This means repeater purging will not work properly.
Check your meshcore-cli installation and device connection.
```

---

### `!repeater test-purge`

Tests the improved purge system.

**Usage:**
```
!repeater test-purge
```

**What it does:**
- Performs a test purge operation
- Shows initial and final contact counts
- Reports which purge method was used
- Verifies the purge system is working

**Example Response:**
```
🧪 Test: TestContact | 50→49 (-1) | contact_key | ✅ OK
```

---

### `!repeater debug-purge`

Debugs the purge system to see what repeaters are available.

**Usage:**
```
!repeater debug-purge
```

**What it shows:**
- Total contacts on device
- Number of repeaters found
- List of repeaters with their types
- Test of purge selection logic

**Example Response:**
```
🔍 Debug: 50 total, 12 repeaters | • Hillcrest (Repeater)... | • Northgate (RoomServer)... | Test: 3 available
```

---

## Help

### `!repeater help`

Shows comprehensive help for all repeater commands.

**Usage:**
```
!repeater help
```

---

## Configuration

The repeater management system respects several configuration settings in `config.ini`:

- `auto_manage_contacts` - Controls automatic contact management (default: `device`)
  - `device` - Device handles auto-addition, bot manages capacity (default)
  - `bot` - Bot automatically adds companion contacts and manages capacity
  - `false` - Manual mode (use commands to manage contacts)

- Auto-purge threshold and limits are configured in the repeater manager

---

## Notes

1. **DM Only**: All repeater commands are DM-only for security reasons
2. **Rate Limiting**: Geolocation commands respect rate limits (2 seconds between requests)
3. **Database Tracking**: All actions are logged in the database with reasons
4. **Verification**: Purge operations verify successful removal
5. **Background Tasks**: Some operations (like background geocoding) run asynchronously
6. **Multi-Message Responses**: Some commands (like `locations`) may send multiple messages to stay within message size limits

---

## Common Use Cases

### Initial Setup
```
!repeater scan                    # Catalog existing repeaters
!repeater status                  # Check contact list capacity
```

### Regular Maintenance
```
!repeater status                  # Check capacity
!repeater manage                  # Auto-manage if needed
!repeater update-geo              # Fill in missing location data
```

### Emergency Cleanup
```
!repeater status                  # Check if at limit
!repeater purge-status            # Detailed status
!repeater auto-purge trigger      # Trigger auto-purge
```

### Bulk Operations
```
!repeater purge 30                # Remove old repeaters
!repeater update-geo 20           # Geocode 20 repeaters
!repeater geocode bulk 25         # Bulk geocode 25 contacts
```

---

## Error Messages

Common error messages and their meanings:

- `Repeater manager not initialized` - Bot configuration issue, check logs
- `No repeaters found` - No repeaters match your criteria
- `Failed to purge` - Contact removal failed (try `force` option)
- `No network connectivity` - Cannot reach geocoding service
- `Rate limited` - Too many geocoding requests (wait and retry)

---

## See Also

- `REPEATER_MANAGEMENT.md` - Technical details about repeater management system
- Bot configuration file (`config.ini`) - Configuration options

