import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import asyncio
from typing import Optional, Dict, List
import json
import requests
import os
import webserver

# Bot configuration
TOKEN = None  # Set this through environment variables
PREFIX = '-'

# XP Level thresholds
LEVEL_THRESHOLDS = {
    1: 0,
    2: 100,
    3: 500,
    4: 1200,
    5: 2200,
    6: 3500,
    7: 5100,
    8: 7000,
    9: 9200,
    10: 11700
}

# Bot setup - With message content intent for full functionality
# NOTE: Requires "Message Content Intent" enabled in Discord Developer Portal
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True
intents.message_content = True  # Privileged intent - enable in Discord Developer Portal
intents.members = True  # Privileged intent - enable in Discord Developer Portal to read member roles

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

class QuestBot:
    def __init__(self):
        self.db_connection = None
        self.quest_ping_role_id = None
        self.quest_channel_id = None
        self.role_xp_assignments = {}
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database for storing user XP and quest data"""
        self.db_connection = sqlite3.connect('quest_bot.db')
        cursor = self.db_connection.cursor()
        
        # Create users table for XP tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                UNIQUE(user_id, guild_id)
            )
        ''')
        
        # Create quests table for active quests
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quests (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                title TEXT,
                content TEXT,
                completed_users TEXT DEFAULT '[]'
            )
        ''')
        
        # Create settings table for bot configuration
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                quest_ping_role_id INTEGER,
                quest_channel_id INTEGER,
                role_xp_assignments TEXT DEFAULT '{}'
            )
        ''')
        
        # Create streak_role_gains table for tracking streak role accumulation
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS streak_role_gains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                role_id INTEGER,
                role_name TEXT,
                xp_awarded INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.db_connection.commit()
    
    def get_user_data(self, user_id: int, guild_id: int):
        """Get user XP and level data"""
        if not self.db_connection:
            return {'xp': 0, 'level': 1}
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT xp, level FROM users WHERE user_id = ? AND guild_id = ?', (user_id, guild_id))
        result = cursor.fetchone()
        if result:
            return {'xp': result[0], 'level': result[1]}
        else:
            # Create new user entry
            cursor.execute('INSERT INTO users (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)', (user_id, guild_id))
            self.db_connection.commit()
            return {'xp': 0, 'level': 1}
    
    def update_user_xp(self, user_id: int, guild_id: int, xp_change: int):
        """Update user base XP and recalculate level based on total XP"""
        if not self.db_connection:
            return 0, 1
        cursor = self.db_connection.cursor()
        current_data = self.get_user_data(user_id, guild_id)
        old_level = current_data['level']
        new_base_xp = max(0, current_data['xp'] + xp_change)
        
        # Update base XP in database first
        cursor.execute('UPDATE users SET xp = ? WHERE user_id = ? AND guild_id = ?', 
                      (new_base_xp, user_id, guild_id))
        self.db_connection.commit()
        
        # Calculate level based on TOTAL XP (including roles), not just base XP
        total_xp = self.calculate_total_user_xp(user_id, guild_id)
        new_level = self.calculate_level(total_xp)
        
        # Update level in database if changed
        if old_level != new_level:
            cursor.execute('UPDATE users SET level = ? WHERE user_id = ? AND guild_id = ?', 
                          (new_level, user_id, guild_id))
            self.db_connection.commit()
            asyncio.create_task(self.update_user_level_role(user_id, guild_id, old_level, new_level))
        
        return total_xp, new_level
    
    async def create_level_roles(self, guild):
        """Create level roles if they don't exist"""
        try:
            for level in range(1, 11):
                role_name = f"Level {level}"
                # Check if role already exists
                existing_role = discord.utils.get(guild.roles, name=role_name)
                if not existing_role:
                    # Create role with a color gradient from blue to gold
                    color_value = int(0x0099ff + (0xffd700 - 0x0099ff) * (level - 1) / 9)
                    await guild.create_role(
                        name=role_name,
                        color=discord.Color(color_value),
                        reason=f"Auto-created level role for Level {level}"
                    )
                    print(f"Created role: {role_name}")
        except discord.Forbidden:
            print("Bot lacks permission to create roles")
        except Exception as e:
            print(f"Error creating level roles: {e}")
    
    async def update_user_level_role(self, user_id: int, guild_id: int, old_level: int, new_level: int):
        """Update user's level role when they level up/down"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                print(f"Guild {guild_id} not found")
                return
            
            member = guild.get_member(user_id)
            if not member:
                print(f"Member {user_id} not found in guild {guild_id}")
                return
            
            # Remove ALL existing level roles (not just the old one)
            removed_roles = []
            for role in member.roles:
                if role.name.startswith("Level ") and role.name != f"Level {new_level}":
                    removed_roles.append(role.name)
                    await member.remove_roles(role, reason="Level changed - removing old level role")
            
            # Add new level role
            new_role_name = f"Level {new_level}"
            new_role = discord.utils.get(guild.roles, name=new_role_name)
            if new_role:
                if new_role not in member.roles:
                    await member.add_roles(new_role, reason=f"Reached {new_role_name}")
                    print(f"✅ {member.display_name}: Removed {removed_roles} → Added {new_role_name}")
                else:
                    print(f"ℹ️ {member.display_name}: Already has {new_role_name}, removed {removed_roles}")
            else:
                # Create the role if it doesn't exist
                print(f"Creating missing level roles...")
                await self.create_level_roles(guild)
                new_role = discord.utils.get(guild.roles, name=new_role_name)
                if new_role:
                    await member.add_roles(new_role, reason=f"Reached {new_role_name}")
                    print(f"✅ {member.display_name}: Created and added {new_role_name}")
                else:
                    print(f"❌ Failed to create {new_role_name}")
        except discord.Forbidden as e:
            print(f"❌ Bot lacks permission to manage roles: {e}")
            print(f"   Make sure bot role is higher than Level roles in server settings!")
        except Exception as e:
            print(f"❌ Error updating user level role: {e}")
    
    def calculate_level(self, xp: int) -> int:
        """Calculate level based on XP"""
        for level in range(10, 0, -1):
            if xp >= LEVEL_THRESHOLDS[level]:
                return level
        return 1
    
    def calculate_total_user_xp(self, user_id: int, guild_id: int) -> int:
        """Calculate total XP including quest XP + role-based XP"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                print(f"Guild {guild_id} not found")
                user_data = self.get_user_data(user_id, guild_id)
                return user_data.get('xp', 0)
            
            member = guild.get_member(user_id)
            if not member:
                print(f"Member {user_id} not found in guild {guild_id}")
                user_data = self.get_user_data(user_id, guild_id)
                return user_data.get('xp', 0)
            
            # Get base XP from database (quest completions and manual additions)
            user_data = self.get_user_data(user_id, guild_id)
            base_xp = user_data.get('xp', 0)
            
            # Add XP from custom assigned roles (excluding streak roles which use accumulated system)
            custom_role_xp = 0
            for role in member.roles:
                # Skip level roles - they don't contribute to XP calculation to avoid circular dependency
                if role.name.startswith("Level "):
                    continue
                    
                role_xp_data = self.get_role_xp_and_type(guild_id, str(role.id))
                if role_xp_data:
                    xp_amount, role_type = role_xp_data
                    # Skip streak roles since they now use accumulated XP system
                    if role_type != "streak":
                        custom_role_xp += xp_amount
            
            # Add XP from accumulated streak roles (historical gains)
            accumulated_streak_xp = self.get_accumulated_streak_xp(user_id, guild_id)
            
            # Add XP from current badge roles (only for unassigned roles that have "badge" in name)
            auto_role_xp = 0
            badge_roles_found = []
            for role in member.roles:
                # Skip level roles - they don't contribute to XP calculation
                if role.name.startswith("Level "):
                    continue
                    
                role_name_lower = role.name.lower()
                role_id_str = str(role.id)
                role_xp_data = self.get_role_xp_and_type(guild_id, role_id_str)
                # Only apply auto-detection fallback if role doesn't have explicit assignment
                if not role_xp_data:
                    # Badge roles give 5 XP each (fallback for unassigned roles)
                    if "badge" in role_name_lower:
                        auto_role_xp += 5
                        badge_roles_found.append(role.name)
                    # Note: Streak roles now use accumulated XP instead of current roles
            
            # Calculate total XP - sum of base XP + all role bonuses + accumulated streak XP
            # NO level role XP to avoid circular dependency in level calculation
            total_xp = base_xp + custom_role_xp + auto_role_xp + accumulated_streak_xp
            
            # Log badge roles found for debugging
            if badge_roles_found:
                print(f"Role XP for {member.display_name}: Badge roles: {badge_roles_found}, Auto XP: {auto_role_xp}")
            if accumulated_streak_xp > 0:
                print(f"Accumulated Streak XP for {member.display_name}: {accumulated_streak_xp}")
            
            return total_xp
            
        except Exception as e:
            print(f"Error calculating total XP for user {user_id}: {e}")
            import traceback
            traceback.print_exc()
            # Fall back to database XP
            user_data = self.get_user_data(user_id, guild_id)
            return user_data.get('xp', 0)
    
    def get_leaderboard(self, guild_id: int, limit: int = 10):
        """Get top users for leaderboard with total XP including roles"""
        if not self.db_connection:
            return []
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT user_id, xp, level FROM users WHERE guild_id = ? ORDER BY xp DESC', (guild_id,))
        all_users = cursor.fetchall()
        
        # Calculate total XP for each user (including role bonuses)
        users_with_total_xp = []
        for user_id, base_xp, level in all_users:
            total_xp = self.calculate_total_user_xp(user_id, guild_id)
            new_level = self.calculate_level(total_xp)
            users_with_total_xp.append((user_id, total_xp, new_level))
        
        # Sort by total XP and limit results
        users_with_total_xp.sort(key=lambda x: x[1], reverse=True)
        return users_with_total_xp[:limit]
    
    def save_settings(self, guild_id: int):
        """Save bot settings to database"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        role_xp_json = json.dumps(self.role_xp_assignments.get(guild_id, {}))
        cursor.execute('''
            INSERT OR REPLACE INTO settings 
            (guild_id, quest_ping_role_id, quest_channel_id, role_xp_assignments) 
            VALUES (?, ?, ?, ?)
        ''', (guild_id, self.quest_ping_role_id, self.quest_channel_id, role_xp_json))
        self.db_connection.commit()
    
    def load_settings(self, guild_id: int):
        """Load bot settings from database"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT quest_ping_role_id, quest_channel_id, role_xp_assignments FROM settings WHERE guild_id = ?', (guild_id,))
        result = cursor.fetchone()
        if result:
            self.quest_ping_role_id = result[0]
            self.quest_channel_id = result[1]
            loaded_assignments = json.loads(result[2])
            
            # Migrate old format to new format if needed
            migrated_assignments = {}
            for role_id, data in loaded_assignments.items():
                if isinstance(data, int):
                    # Old format: role_id -> xp_amount
                    # Migrate to new format: role_id -> {"xp": xp_amount, "type": "badge"}
                    # Default to "badge" for backward compatibility
                    migrated_assignments[role_id] = {"xp": data, "type": "badge"}
                else:
                    # New format: role_id -> {"xp": xp_amount, "type": "streak"|"badge"}
                    migrated_assignments[role_id] = data
            
            self.role_xp_assignments[guild_id] = migrated_assignments
    
    def record_streak_role_gain(self, user_id: int, guild_id: int, role_id: int, role_name: str, xp_awarded: int):
        """Record when a user gains a streak role for accumulation tracking"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        cursor.execute('''
            INSERT INTO streak_role_gains (user_id, guild_id, role_id, role_name, xp_awarded)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, guild_id, role_id, role_name, xp_awarded))
        self.db_connection.commit()
        print(f"Recorded streak role gain: {role_name} (+{xp_awarded} XP) for user {user_id}")
    
    def get_accumulated_streak_xp(self, user_id: int, guild_id: int) -> int:
        """Get total accumulated streak XP from all historical role gains"""
        if not self.db_connection:
            return 0
        cursor = self.db_connection.cursor()
        cursor.execute('''
            SELECT SUM(xp_awarded) FROM streak_role_gains 
            WHERE user_id = ? AND guild_id = ?
        ''', (user_id, guild_id))
        result = cursor.fetchone()
        return result[0] if result[0] else 0
    
    def get_role_xp_and_type(self, guild_id: int, role_id: str):
        """Get XP amount and type for a role, returns (xp, type) or None if not assigned"""
        if guild_id not in self.role_xp_assignments:
            return None
        role_data = self.role_xp_assignments[guild_id].get(role_id)
        if role_data:
            return role_data["xp"], role_data["type"]
        return None
    
    def assign_role_xp(self, guild_id: int, role_id: str, xp_amount: int, role_type: str):
        """Assign XP and type to a role"""
        if guild_id not in self.role_xp_assignments:
            self.role_xp_assignments[guild_id] = {}
        self.role_xp_assignments[guild_id][role_id] = {"xp": xp_amount, "type": role_type}

quest_bot = QuestBot()

@bot.event
async def on_ready():
    print(f'{bot.user} has logged in to Discord!')
    for guild in bot.guilds:
        quest_bot.load_settings(guild.id)
        # Create level roles on startup
        await quest_bot.create_level_roles(guild)
        # Cache members to improve role reading
        try:
            await guild.chunk()
            print(f"Cached {guild.member_count} members for {guild.name}")
        except Exception as e:
            print(f"Failed to cache members for {guild.name}: {e}")
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

@bot.event
async def on_reaction_add(reaction, user):
    """Handle quest completion reactions"""
    if user.bot:
        return
    
    # Check if it's a quest completion (✅ emoji)
    if str(reaction.emoji) == '✅':
        if not quest_bot.db_connection:
            return
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('SELECT title, completed_users FROM quests WHERE message_id = ?', (reaction.message.id,))
        quest_data = cursor.fetchone()
        
        if quest_data:
            title, completed_users_json = quest_data
            completed_users = json.loads(completed_users_json)
            
            if user.id not in completed_users:
                # Award 50 XP for quest completion
                new_xp, new_level = quest_bot.update_user_xp(user.id, reaction.message.guild.id, 50)
                completed_users.append(user.id)
                
                # Update quest completion list
                cursor.execute('UPDATE quests SET completed_users = ? WHERE message_id = ?', 
                              (json.dumps(completed_users), reaction.message.id))
                if quest_bot.db_connection:
                    quest_bot.db_connection.commit()
                
                # Send confirmation message
                embed = discord.Embed(
                    title="Quest Completed!",
                    description=f"{user.mention} completed: **{title}**\n+50 XP (Total: {new_xp} XP, Level {new_level})",
                    color=0x00ff00
                )
                await reaction.message.channel.send(embed=embed, delete_after=10)

async def check_and_update_level_roles(user_id: int, guild_id: int, reason: str = "XP change"):
    """Comprehensive level role check and update function"""
    try:
        # Get current level in database
        current_data = quest_bot.get_user_data(user_id, guild_id)
        old_level = current_data['level']
        
        # Calculate actual total XP and new level
        current_total_xp = quest_bot.calculate_total_user_xp(user_id, guild_id)
        new_level = quest_bot.calculate_level(current_total_xp)
        
        # Update level in database if changed and trigger level role assignment
        if old_level != new_level:
            cursor = quest_bot.db_connection.cursor()
            cursor.execute('UPDATE users SET level = ? WHERE user_id = ? AND guild_id = ?', 
                          (new_level, user_id, guild_id))
            quest_bot.db_connection.commit()
            asyncio.create_task(quest_bot.update_user_level_role(user_id, guild_id, old_level, new_level))
            return old_level, new_level, current_total_xp
        
        return old_level, old_level, current_total_xp
    except Exception as e:
        print(f"Error in check_and_update_level_roles: {e}")
        return 1, 1, 0

@bot.event
async def on_member_update(before, after):
    """Handle role changes for automatic XP assignment"""
    guild_id = after.guild.id
    
    # Check for role changes (additions OR removals)
    added_roles = set(after.roles) - set(before.roles)
    removed_roles = set(before.roles) - set(after.roles)
    
    # Handle specific role additions
    for role in added_roles:
        role_xp_data = quest_bot.get_role_xp_and_type(guild_id, str(role.id))
        if role_xp_data:
            xp_reward, role_type = role_xp_data
            
            # Handle streak roles differently - accumulate each time they're gained
            if role_type == "streak":
                quest_bot.record_streak_role_gain(after.id, guild_id, role.id, role.name, xp_reward)
                
                # Check for level changes after streak accumulation
                old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "streak role gain")
                level_text = f" → Level {new_level}!" if old_level != new_level else ""
                
                # Send notification for streak role gain
                embed = discord.Embed(
                    title="🔥 Streak Role Gained!",
                    description=f"{after.mention} gained **{role.name}** role!\n+{xp_reward} Streak XP accumulated (Total: {total_xp} XP){level_text}",
                    color=0xff6600
                )
            else:
                # Check for level changes after badge role gain
                old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "badge role gain")
                level_text = f" → Level {new_level}!" if old_level != new_level else ""
                
                # Send notification for badge role gain
                embed = discord.Embed(
                    title="🏅 Role Gained!",
                    description=f"{after.mention} gained **{role.name}** role!\n+{xp_reward} XP (Total: {total_xp} XP){level_text}",
                    color=0x0099ff
                )
            
            # Try to send to general channel or first available channel
            for channel in after.guild.text_channels:
                if hasattr(channel, 'send') and channel.permissions_for(after.guild.me).send_messages:
                    await channel.send(embed=embed, delete_after=15)
                    break
        elif "badge" in role.name.lower():
            # Handle unassigned badge roles (fallback +5 XP)
            old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "badge role gain")
            level_text = f" → Level {new_level}!" if old_level != new_level else ""
            
            embed = discord.Embed(
                title="🏅 Role Gained!",
                description=f"{after.mention} gained **{role.name}** role!\n+5 XP (Total: {total_xp} XP){level_text}",
                color=0x0099ff
            )
            
            # Try to send to general channel or first available channel
            for channel in after.guild.text_channels:
                if hasattr(channel, 'send') and channel.permissions_for(after.guild.me).send_messages:
                    await channel.send(embed=embed, delete_after=15)
                    break
    
    # Check for level changes after any role removals (could lower total XP)
    if removed_roles:
        # Check if any removed roles had XP impact
        has_xp_impact = False
        for role in removed_roles:
            role_xp_data = quest_bot.get_role_xp_and_type(guild_id, str(role.id))
            if role_xp_data or "badge" in role.name.lower():
                has_xp_impact = True
                break
        
        if has_xp_impact:
            old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "role removal")
            # Note: We don't send notifications for role removals as they might be sensitive

@bot.command(name='addquest')
@commands.has_any_role('staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN')
async def add_quest(ctx, title: str, *, content: str):
    """Add a new quest embed"""
    embed = discord.Embed(
        title=f"🎯 Quest: {title}",
        description=content,
        color=0xff9900
    )
    embed.add_field(name="Reward", value="50 XP", inline=True)
    embed.add_field(name="Complete", value="React with ✅", inline=True)
    embed.set_footer(text="React with ✅ to mark this quest as complete!")
    
    # Send to quest channel if set, otherwise current channel
    channel_id = quest_bot.quest_channel_id
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            quest_message = await channel.send(embed=embed)
        else:
            quest_message = await ctx.send(embed=embed)
    else:
        quest_message = await ctx.send(embed=embed)
    
    # Add checkmark reaction
    await quest_message.add_reaction('✅')
    
    # Ping quest role - first check manual setting, then auto-find @Quests role
    quest_role = None
    if quest_bot.quest_ping_role_id:
        quest_role = ctx.guild.get_role(quest_bot.quest_ping_role_id)
    
    if not quest_role:
        # Auto-find @Quests role
        quest_role = discord.utils.get(ctx.guild.roles, name="Quests")
    
    if quest_role:
        ping_msg = await quest_message.channel.send(f"{quest_role.mention} New quest available!")
        await asyncio.sleep(2)
        await ping_msg.delete()
    
    # Save quest to database
    if quest_bot.db_connection:
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('INSERT INTO quests (message_id, guild_id, channel_id, title, content) VALUES (?, ?, ?, ?, ?)',
                      (quest_message.id, ctx.guild.id, quest_message.channel.id, title, content))
        quest_bot.db_connection.commit()
    
    await ctx.message.delete()

@bot.command(name='removequest')
@commands.has_any_role('staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN')
async def remove_quest(ctx, message_id: int):
    """Remove a quest by message ID"""
    try:
        # Remove from database
        if quest_bot.db_connection:
            cursor = quest_bot.db_connection.cursor()
            cursor.execute('DELETE FROM quests WHERE message_id = ?', (message_id,))
            quest_bot.db_connection.commit()
        
        # Try to delete the message
        try:
            message = await ctx.fetch_message(message_id)
            await message.delete()
        except:
            pass
        
        await ctx.send("✅ Quest removed successfully!", delete_after=5)
    except Exception as e:
        await ctx.send("❌ Failed to remove quest. Make sure the message ID is correct.", delete_after=5)

@bot.command(name='questping')
@commands.has_permissions(manage_roles=True)
async def set_quest_ping(ctx, role_id: int):
    """Set the role to ping for new quests"""
    role = ctx.guild.get_role(role_id)
    if role:
        quest_bot.quest_ping_role_id = role_id
        quest_bot.save_settings(ctx.guild.id)
        await ctx.send(f"✅ Quest ping role set to: {role.mention}", delete_after=5)
    else:
        await ctx.send("❌ Role not found!", delete_after=5)

@bot.command(name='questchannel')
@commands.has_permissions(manage_channels=True)
async def set_quest_channel(ctx, channel_id: int):
    """Set the channel for quest embeds"""
    channel = bot.get_channel(channel_id)
    if channel:
        quest_bot.quest_channel_id = channel_id
        quest_bot.save_settings(ctx.guild.id)
        await ctx.send(f"✅ Quest channel set to: {channel.mention}", delete_after=5)
    else:
        await ctx.send("❌ Channel not found!", delete_after=5)

@bot.command(name='addXP')
@commands.has_any_role('staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN')
async def add_xp(ctx, member: discord.Member, amount: int):
    """Add XP to a member (in increments of 5)"""
    # Enforce 5 XP increments
    if amount % 5 != 0:
        embed = discord.Embed(
            title="❌ Invalid XP Amount",
            description=f"XP must be added in increments of 5.\nTry: 5, 10, 15, 20, 25, 50, etc.",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    new_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, amount)
    
    # Get total XP including role bonuses (same as leaderboard calculation)
    total_xp = quest_bot.calculate_total_user_xp(member.id, ctx.guild.id)
    total_level = quest_bot.calculate_level(total_xp)
    
    embed = discord.Embed(
        title="XP Added",
        description=f"Added {amount} XP to {member.mention}\nNew Total: {total_xp:,} XP (Level {total_level})",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@bot.command(name='removeXP')
@commands.has_any_role('staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN')
async def remove_xp(ctx, member: discord.Member, amount: int):
    """Remove XP from a member (in increments of 5)"""
    # Enforce 5 XP increments
    if amount % 5 != 0:
        embed = discord.Embed(
            title="❌ Invalid XP Amount",
            description=f"XP must be removed in increments of 5.\nTry: 5, 10, 15, 20, 25, 50, etc.",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    new_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, -amount)
    
    # Get total XP including role bonuses (same as leaderboard calculation)
    total_xp = quest_bot.calculate_total_user_xp(member.id, ctx.guild.id)
    total_level = quest_bot.calculate_level(total_xp)
    
    embed = discord.Embed(
        title="XP Removed",
        description=f"Removed {amount} XP from {member.mention}\nNew Total: {total_xp:,} XP (Level {total_level})",
        color=0xff0000
    )
    await ctx.send(embed=embed)

@bot.command(name='setXP')
@commands.has_any_role('staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN')
async def set_xp(ctx, member: discord.Member, amount: int):
    """Set a member's XP to a specific amount (in increments of 5)"""
    # Enforce 5 XP increments
    if amount % 5 != 0:
        embed = discord.Embed(
            title="❌ Invalid XP Amount",
            description=f"XP must be set in increments of 5.\nTry: 0, 5, 10, 15, 20, 25, 50, etc.",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    # Ensure amount is not negative
    if amount < 0:
        embed = discord.Embed(
            title="❌ Invalid XP Amount",
            description="XP cannot be set to a negative value.\nMinimum: 0 XP",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    # Set XP directly by calculating the difference from current XP
    current_data = quest_bot.get_user_data(member.id, ctx.guild.id)
    current_xp = current_data['xp']
    xp_difference = amount - current_xp
    
    new_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, xp_difference)
    
    # Get total XP including role bonuses (same as leaderboard calculation)
    total_xp = quest_bot.calculate_total_user_xp(member.id, ctx.guild.id)
    total_level = quest_bot.calculate_level(total_xp)
    
    embed = discord.Embed(
        title="XP Set",
        description=f"Set {member.mention}'s base XP to {amount}\nTotal XP: {total_xp:,} (Level {total_level})",
        color=0x0099ff
    )
    await ctx.send(embed=embed)

@bot.command(name='assignbadgeXP')
@commands.has_permissions(manage_roles=True)
async def assign_badge_xp(ctx, xp_amount: int, *roles: discord.Role):
    """Assign XP value to badge roles - auto-detects if no roles specified"""
    guild_id = ctx.guild.id
    if guild_id not in quest_bot.role_xp_assignments:
        quest_bot.role_xp_assignments[guild_id] = {}
    
    # If roles are provided, use those; otherwise show usage message
    if roles:
        badge_roles = list(roles)
    else:
        embed = discord.Embed(
            title="❌ No Roles Specified",
            description="Please specify which roles should be badge roles.\n\n**Usage:** `-assignbadgeXP 5 @role1 @role2`\n\n*The system will remember that these roles are badge roles for XP tracking.*",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    # List found badge roles and assign XP
    assigned_count = 0
    role_list = ""
    
    for role in badge_roles:
        role_id_str = str(role.id)
        
        # Check if already assigned
        existing_data = quest_bot.get_role_xp_and_type(guild_id, role_id_str)
        if not existing_data:
            quest_bot.assign_role_xp(guild_id, role_id_str, xp_amount, "badge")
            assigned_count += 1
            role_list += f"• **{role.name}** - {xp_amount} Badge XP\n"
        else:
            current_xp, current_type = existing_data
            role_list += f"• **{role.name}** - Already assigned {current_xp} {current_type.title()} XP (skipped)\n"
    
    quest_bot.save_settings(guild_id)
    
    mode_text = "auto-detected" if detection_mode == "auto" else "specified"
    embed = discord.Embed(
        title="🏅 Badge Role XP Assignment",
        description=f"Found {len(badge_roles)} {mode_text} badge role(s). Assigned XP to {assigned_count} new role(s):",
        color=0x00ff00
    )
    
    if role_list:
        embed.add_field(name="Badge Roles", value=role_list[:1024], inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='assignstreakXP')
@commands.has_permissions(manage_roles=True)
async def assign_streak_xp(ctx, xp_amount: int, *roles: discord.Role):
    """Assign XP value to streak roles - auto-detects if no roles specified"""
    guild_id = ctx.guild.id
    if guild_id not in quest_bot.role_xp_assignments:
        quest_bot.role_xp_assignments[guild_id] = {}
    
    # If roles are provided, use those; otherwise show usage message
    if roles:
        streak_roles = list(roles)
    else:
        embed = discord.Embed(
            title="❌ No Roles Specified",
            description="Please specify which roles should be streak roles.\n\n**Usage:** `-assignstreakXP 10 @1week @2weeks @1month`\n\n*The system will remember that these roles are streak roles for XP tracking.*",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    # List found streak roles and assign XP
    assigned_count = 0
    role_list = ""
    
    for role in streak_roles:
        role_id_str = str(role.id)
        
        # Check if already assigned
        existing_data = quest_bot.get_role_xp_and_type(guild_id, role_id_str)
        if not existing_data:
            quest_bot.assign_role_xp(guild_id, role_id_str, xp_amount, "streak")
            assigned_count += 1
            role_list += f"• **{role.name}** - {xp_amount} Streak XP\n"
        else:
            current_xp, current_type = existing_data
            role_list += f"• **{role.name}** - Already assigned {current_xp} {current_type.title()} XP (skipped)\n"
    
    quest_bot.save_settings(guild_id)
    
    mode_text = "auto-detected" if detection_mode == "auto" else "specified"
    embed = discord.Embed(
        title="🔥 Streak Role XP Assignment",
        description=f"Found {len(streak_roles)} {mode_text} streak role(s). Assigned XP to {assigned_count} new role(s):",
        color=0x00ff00
    )
    
    if role_list:
        embed.add_field(name="Streak Roles", value=role_list[:1024], inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='unassignroleXP')
@commands.has_permissions(manage_roles=True)
async def unassign_role_xp(ctx, *roles: discord.Role):
    """Remove XP assignment from multiple roles (staff only)"""
    guild_id = ctx.guild.id
    
    if not roles:
        embed = discord.Embed(
            title="❌ No Roles Specified",
            description="Please specify one or more roles to unassign XP from.\n\n**Usage:** `-unassignroleXP @role1 @role2 @role3`",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    # Check if guild has any role assignments
    if guild_id not in quest_bot.role_xp_assignments:
        embed = discord.Embed(
            title="❌ No Role Assignments",
            description="No roles in this server have XP assignments to remove.",
            color=0xff9900
        )
        await ctx.send(embed=embed)
        return
    
    role_assignments = quest_bot.role_xp_assignments[guild_id]
    unassigned_count = 0
    role_list = ""
    
    for role in roles:
        role_id_str = str(role.id)
        
        # Check if this specific role has an assignment
        if role_id_str in role_assignments:
            old_assignment = role_assignments[role_id_str]
            # Handle both old format (int) and new format (dict)
            if isinstance(old_assignment, dict):
                old_xp = old_assignment.get('xp', 0)
                old_type = old_assignment.get('type', 'badge')
                del role_assignments[role_id_str]
                unassigned_count += 1
                role_list += f"• **{role.name}** - Removed {old_xp} XP assignment ({old_type} role)\n"
            else:
                # Old format compatibility
                del role_assignments[role_id_str]
                unassigned_count += 1
                role_list += f"• **{role.name}** - Removed {old_assignment} XP assignment\n"
        else:
            role_list += f"• **{role.name}** - No XP assignment found (skipped)\n"
    
    # Save changes to database
    if unassigned_count > 0:
        quest_bot.save_settings(guild_id)
    
    embed = discord.Embed(
        title="🗑️ Role XP Unassignment",
        description=f"Processed {len(roles)} role(s). Removed XP assignments from {unassigned_count} role(s):",
        color=0x00ff00 if unassigned_count > 0 else 0xff9900
    )
    
    if role_list:
        embed.add_field(name="Results", value=role_list[:1024], inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='checkroleXP')
async def check_role_xp(ctx, role: discord.Role):
    """Display the XP amount assigned to a role"""
    guild_id = ctx.guild.id
    
    # Check if guild has any role assignments
    if guild_id not in quest_bot.role_xp_assignments:
        embed = discord.Embed(
            title="🔍 Role XP Check",
            description=f"Role **{role.name}** has no XP assignment.\n\nUse `-assignroleXP @{role.name} <amount>` to assign XP to this role.",
            color=0xff9900
        )
        await ctx.send(embed=embed)
        return
    
    role_assignments = quest_bot.role_xp_assignments[guild_id]
    role_id_str = str(role.id)
    
    # Check if this specific role has an assignment
    if role_id_str not in role_assignments:
        embed = discord.Embed(
            title="🔍 Role XP Check",
            description=f"Role **{role.name}** has no XP assignment.\n\nUse `-assignroleXP @{role.name} <amount>` to assign XP to this role.",
            color=0xff9900
        )
        await ctx.send(embed=embed)
        return
    
    # Display the role's XP assignment
    assignment = role_assignments[role_id_str]
    
    # Handle both old format (int) and new format (dict)
    if isinstance(assignment, dict):
        xp_amount = assignment.get('xp', 0)
        role_type = assignment.get('type', 'badge')
        embed = discord.Embed(
            title="🔍 Role XP Check",
            description=f"Role **{role.name}** awards **{xp_amount} XP** when obtained by users.",
            color=0x00ff00
        )
        embed.add_field(
            name="Role Info",
            value=f"**Role:** {role.mention}\n**XP Value:** {xp_amount} XP\n**Role Type:** {role_type}\n**Role ID:** {role.id}",
            inline=False
        )
    else:
        # Old format compatibility
        embed = discord.Embed(
            title="🔍 Role XP Check",
            description=f"Role **{role.name}** awards **{assignment} XP** when obtained by users.",
            color=0x00ff00
        )
        embed.add_field(
            name="Role Info",
            value=f"**Role:** {role.mention}\n**XP Value:** {assignment} XP\n**Role ID:** {role.id}",
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Display the XP leaderboard"""
    try:
        leaderboard_data = quest_bot.get_leaderboard(ctx.guild.id, 10)
        print(f"Leaderboard data retrieved: {leaderboard_data}")  # Debug print
        
        if not leaderboard_data:
            embed = discord.Embed(
                title="🏆 XP Leaderboard",
                description="No users with XP found yet!\nComplete some quests to get on the leaderboard!",
                color=0xffd700
            )
            # Still show level requirements
            level_info = "**Level Requirements:**\n"
            for level, xp in LEVEL_THRESHOLDS.items():
                level_info += f"Level {level}: {xp:,} XP\n"
            embed.add_field(name="Level System", value=level_info, inline=False)
            await ctx.send(embed=embed)
            return
        
        embed = discord.Embed(
            title="🏆 XP Leaderboard",
            description="Top 10 Quest Completers",
            color=0xffd700
        )
        
        medals = ["🥇", "🥈", "🥉"]
        users_added = 0
        
        for i, (user_id, xp, level) in enumerate(leaderboard_data):
            medal = medals[i] if i < 3 else f"#{i+1}"
            
            # Try multiple methods to get user info
            user = ctx.guild.get_member(user_id)
            if not user:
                user = bot.get_user(user_id)
            
            # Calculate total XP including role-based XP
            total_xp = quest_bot.calculate_total_user_xp(user_id, ctx.guild.id)
            
            if user:
                # Format username without pinging - use @ but escape it
                username = f"@{user.name}"
                display_name = getattr(user, 'display_name', user.name)
                if display_name != user.name:
                    username = f"@{user.name} ({display_name})"
                
                embed.add_field(
                    name=f"{medal} Level {level}",
                    value=f"{username}\n{total_xp:,} XP",
                    inline=True
                )
                users_added += 1
            else:
                # Try to fetch user info from Discord API
                try:
                    user = await bot.fetch_user(user_id)
                    username = f"@{user.name}"
                    embed.add_field(
                        name=f"{medal} Level {level}",
                        value=f"{username}\n{total_xp:,} XP",
                        inline=True
                    )
                    users_added += 1
                except:
                    # Last resort - show user ID
                    embed.add_field(
                        name=f"{medal} Level {level}",
                        value=f"@User{str(user_id)[-4:]}\n{total_xp:,} XP",
                        inline=True
                    )
                    users_added += 1
        
        if users_added == 0:
            embed.add_field(
                name="No Active Users", 
                value="Users with XP may have left the server", 
                inline=False
            )
        
        # Add level requirements info
        level_info = "**Level Requirements:**\n"
        for level, xp_req in LEVEL_THRESHOLDS.items():
            level_info += f"Level {level}: {xp_req:,} XP\n"
        
        embed.add_field(name="Level System", value=level_info, inline=False)
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"Error in leaderboard command: {e}")
        await ctx.send("❌ Could not retrieve leaderboard data. Please try again later.", delete_after=5)

@bot.command(name='allquests')
@commands.guild_only()
async def all_quests(ctx):
    """List all current quests by name"""
    if not quest_bot.db_connection:
        await ctx.send("❌ Database connection error!")
        return
    
    try:
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('SELECT title, message_id, channel_id FROM quests WHERE guild_id = ? ORDER BY title', (ctx.guild.id,))
        quests = cursor.fetchall()
        
        if not quests:
            await ctx.send("📝 No active quests found! Use `-addquest` to create one.")
            return
        
        embed = discord.Embed(
            title="📋 All Active Quests",
            description="Here are all the current quests:",
            color=0x00ff00
        )
        
        quest_list = []
        for i, (title, message_id, channel_id) in enumerate(quests, 1):
            channel = bot.get_channel(channel_id)
            channel_mention = channel.mention if channel else "#unknown-channel"
            # Create direct message link
            message_link = f"https://discord.com/channels/{ctx.guild.id}/{channel_id}/{message_id}"
            quest_list.append(f"**{i}.** [{title}]({message_link}) (in {channel_mention})")
        
        # Split into chunks if too many quests
        if len(quest_list) <= 10:
            embed.add_field(name="Quests", value="\n".join(quest_list), inline=False)
        else:
            # Split into multiple fields if more than 10 quests
            for i in range(0, len(quest_list), 10):
                chunk = quest_list[i:i+10]
                field_name = f"Quests ({i+1}-{min(i+10, len(quest_list))})"
                embed.add_field(name=field_name, value="\n".join(chunk), inline=False)
        
        embed.set_footer(text=f"Total: {len(quests)} active quest(s)")
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"Error fetching quests: {e}")
        await ctx.send("❌ Error retrieving quests!")

@bot.command(name='deleteallquests')
@commands.has_permissions(manage_messages=True)
async def delete_all_quests(ctx):
    """Delete all current quests (admin only)"""
    if not quest_bot.db_connection:
        await ctx.send("❌ Database connection error!")
        return
    
    try:
        # Get all quests for this guild first
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('SELECT message_id, channel_id, title FROM quests WHERE guild_id = ?', (ctx.guild.id,))
        quests = cursor.fetchall()
        
        if not quests:
            await ctx.send("📝 No quests to delete!")
            return
        
        # Send confirmation message
        embed = discord.Embed(
            title="⚠️ Delete All Quests",
            description=f"Are you sure you want to delete **{len(quests)}** quest(s)?\n\nThis action cannot be undone!",
            color=0xff0000
        )
        
        confirmation_msg = await ctx.send(embed=embed)
        await confirmation_msg.add_reaction('✅')
        await confirmation_msg.add_reaction('❌')
        
        def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in ['✅', '❌'] and reaction.message.id == confirmation_msg.id
        
        try:
            reaction, user = await bot.wait_for('reaction_add', timeout=30.0, check=check)
            
            if str(reaction.emoji) == '❌':
                await confirmation_msg.edit(embed=discord.Embed(
                    title="❌ Cancelled",
                    description="Quest deletion cancelled.",
                    color=0x808080
                ))
                await confirmation_msg.clear_reactions()
                return
            
            # Delete quest messages from Discord (attempt)
            deleted_messages = 0
            for message_id, channel_id, title in quests:
                try:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        message = await channel.fetch_message(message_id)
                        await message.delete()
                        deleted_messages += 1
                except:
                    # Continue even if message deletion fails
                    pass
            
            # Delete all quests from database
            cursor.execute('DELETE FROM quests WHERE guild_id = ?', (ctx.guild.id,))
            quest_bot.db_connection.commit()
            
            # Update confirmation message
            embed = discord.Embed(
                title="✅ All Quests Deleted",
                description=f"Successfully deleted **{len(quests)}** quest(s) from database.\n"
                           f"Removed **{deleted_messages}** quest message(s) from Discord.",
                color=0x00ff00
            )
            await confirmation_msg.edit(embed=embed)
            await confirmation_msg.clear_reactions()
            
        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="⏰ Timeout",
                description="Confirmation timed out. No quests were deleted.",
                color=0x808080
            )
            await confirmation_msg.edit(embed=embed)
            await confirmation_msg.clear_reactions()
    
    except Exception as e:
        print(f"Error deleting all quests: {e}")
        await ctx.send("❌ Error deleting quests!")

@bot.command(name='questbot')
async def questbot_ping(ctx):
    """Ping the bot to check if it's online"""
    await ctx.send("online")

@bot.command(name='checkXP')
async def check_xp(ctx, member: discord.Member = None):
    """Check your current XP and level progress"""
    try:
        # If no member specified, check the command user's XP
        target_member = member or ctx.author
        
        # Get XP breakdown for detailed display
        guild = ctx.guild
        guild_id = guild.id
        
        # Use the same XP calculation method as leaderboard for consistency
        current_xp = quest_bot.calculate_total_user_xp(target_member.id, guild_id)
        current_level = quest_bot.calculate_level(current_xp)
        
        # Get base XP and role breakdown for detailed display
        user_data = quest_bot.get_user_data(target_member.id, guild_id)
        base_xp = user_data.get('xp', 0)
        
        # Calculate XP from different role sources for detailed breakdown
        level_role_xp = 0
        badge_xp = 0
        
        # Get accumulated streak XP from historical role gains (independent of member cache)
        streak_xp = quest_bot.get_accumulated_streak_xp(target_member.id, guild_id)
        
        try:
            guild_member = guild.get_member(target_member.id)
            if guild_member:
                # Level role XP
                for role in guild_member.roles:
                    if role.name.startswith("Level "):
                        try:
                            level_num = int(role.name.split("Level ")[1])
                            if 1 <= level_num <= 10:
                                level_role_xp = max(level_role_xp, LEVEL_THRESHOLDS.get(level_num, 0))
                        except:
                            continue
                
                # Calculate XP from current badge roles only
                for role in guild_member.roles:
                    role_id_str = str(role.id)
                    role_name_lower = role.name.lower()
                    
                    # Check if this role has assigned XP and type
                    role_xp_data = quest_bot.get_role_xp_and_type(guild_id, role_id_str)
                    if role_xp_data:
                        xp_amount, role_type = role_xp_data
                        # Only process badge roles (not streak roles)
                        if role_type == "badge":
                            badge_xp += xp_amount
                    else:
                        # Fallback to auto-detection for unassigned roles with "badge" in name
                        if "badge" in role_name_lower:
                            badge_xp += 5  # Default fallback value
        except Exception as role_error:
            print(f"Error processing roles in checkXP: {role_error}")
            pass
        
        # Calculate XP needed for next level
        next_level = min(current_level + 1, 10)  # Cap at level 10
        next_level_xp = LEVEL_THRESHOLDS.get(next_level, LEVEL_THRESHOLDS[10])
        xp_needed = max(0, next_level_xp - current_xp)
        
        # Calculate progress percentage safely
        if current_level < 10:
            current_level_xp = LEVEL_THRESHOLDS.get(current_level, 0)
            xp_range = next_level_xp - current_level_xp
            if xp_range > 0:
                progress_percentage = min(100, max(0, ((current_xp - current_level_xp) / xp_range) * 100))
            else:
                progress_percentage = 100
        else:
            progress_percentage = 100
        
        embed = discord.Embed(
            title=f"📊 {target_member.display_name}'s XP Stats",
            color=0x00ff00
        )
        
        embed.add_field(name="💰 Total XP", value=f"{current_xp:,} XP", inline=True)
        embed.add_field(name="⭐ Current Level", value=f"Level {current_level}", inline=True)
        
        # Add XP breakdown section
        xp_breakdown = ""
        if base_xp > 0:
            xp_breakdown += f"🎯 **Quest XP:** {base_xp:,}\n"
        if badge_xp > 0:
            xp_breakdown += f"🏅 **Badge XP:** {badge_xp:,}\n"
        # Always show Streak XP (even if 0)
        xp_breakdown += f"🔥 **Streak XP:** {streak_xp:,}\n"
        if level_role_xp > 0:
            xp_breakdown += f"📊 **Level Role XP:** {level_role_xp:,}\n"
        
        if xp_breakdown:
            embed.add_field(name="📈 XP Breakdown", value=xp_breakdown.strip(), inline=False)
        else:
            embed.add_field(name="📈 XP Sources", value="No XP earned yet! Complete quests to get started.", inline=False)
        
        if current_level < 10:
            embed.add_field(name="🎯 XP to Next Level", value=f"{xp_needed:,} XP needed", inline=True)
            
            # Progress bar with safe calculation
            progress_bar_length = 20
            filled_length = int(progress_bar_length * progress_percentage / 100)
            filled_length = max(0, min(filled_length, progress_bar_length))  # Clamp values
            bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)
            embed.add_field(
                name="📈 Progress to Next Level", 
                value=f"`{bar}` {progress_percentage:.1f}%", 
                inline=False
            )
        else:
            embed.add_field(name="🏆 Status", value="**MAX LEVEL REACHED!**", inline=True)
        
        # Safe avatar handling
        try:
            if target_member.avatar:
                embed.set_thumbnail(url=target_member.avatar.url)
            else:
                embed.set_thumbnail(url=target_member.default_avatar.url)
        except:
            pass  # Skip thumbnail if there are issues
        
        embed.set_footer(text="Complete quests and gain roles to earn XP!")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        import traceback
        print(f"Error in checkXP command: {e}")
        print(f"Traceback: {traceback.format_exc()}")
        await ctx.send(f"❌ Could not retrieve XP data. Error: {str(e)[:100]}...", delete_after=10)

@bot.command(name='commands')
async def show_commands(ctx):
    """Display all available bot commands organized by permission level"""
    embed = discord.Embed(
        title="🤖 QuestBot Commands",
        description="Commands organized by access level",
        color=0x0099ff
    )
    
    # User Commands (Everyone can use)
    user_commands = """
    `-leaderboard` - Display XP rankings
    `-checkXP [@member]` - Check your or someone's XP with detailed breakdown (base, badge, streak, level role)
    `-allquests` - List all current quests with clickable links
    `-questbot` - Ping bot to check if online
    `-commands` - Show this command list
    
    **Slash equivalents:** `/leaderboard`, `/questbot`
    """
    
    # Staff Commands (Staff/Admin roles required)
    staff_commands = """
    **XP Management:**
    `-addXP <member> <amount>` - Add XP to user
    `-removeXP <member> <amount>` - Remove XP from user  
    `-setXP <member> <amount>` - Set user's XP to specific amount
    `-assignbadgeXP <amount> [@role1] [@role2]...` - Assign XP to badge roles (auto-detects or specify roles; counts from current membership)
    `-assignstreakXP <amount> [@role1] [@role2]...` - Assign XP to streak roles (auto-detects or specify roles; accumulates each time gained)
    `-unassignroleXP [@role1] [@role2]...` - Remove XP assignment from multiple roles
    `-checkroleXP <role>` - Display XP amount assigned to a role
    
    **Quest Management:**
    `-addquest <title> <content>` - Create new quest embed
    `-removequest <message_id>` - Delete quest by message ID
    
    **Slash equivalents:** `/addxp`, `/removexp`, `/setxp`, `/assignbadgexp`, `/assignstreakxp`, `/addquest`, `/removequest`
    """
    
    # Admin Commands (Manage permissions required) 
    admin_commands = """
    `-deleteallquests` - Delete all current quests
    `-questping <role_id>` - Set quest ping role
    `-questchannel <channel_id>` - Set quest channel
    
    **Slash equivalents:** `/questping`, `/questchannel`, `/createlevelroles`, `/assignlevelroles`
    """
    
    embed.add_field(name="👥 User Commands", value=user_commands, inline=False)
    embed.add_field(name="🛡️ Staff Commands", value=staff_commands, inline=False)  
    embed.add_field(name="⚙️ Admin Commands", value=admin_commands, inline=False)
    
    embed.add_field(
        name="🏆 Level System", 
        value="Earn XP by completing quests (50 XP each) and gaining roles!\n🔥 Streak XP accumulates each time you earn streak roles\n🏅 Badge XP counts from current role membership\nLevel roles are automatically assigned based on your total XP.",
        inline=False
    )
    
    embed.set_footer(text="Use either - or / commands • Both work the same way!")
    
    await ctx.send(embed=embed)

# Slash Commands
@bot.tree.command(name="questbot", description="Ping the bot to check if it's online")
async def slash_questbot_ping(interaction: discord.Interaction):
    await interaction.response.send_message("online")

@bot.tree.command(name="addquest", description="Create a new quest embed")
@app_commands.describe(title="Quest title", content="Quest description")
async def slash_add_quest(interaction: discord.Interaction, title: str, content: str):
    # Check if user has staff role
    staff_roles = ['staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN']
    if not any(role.name in staff_roles for role in interaction.user.roles):
        await interaction.response.send_message("❌ You need the @staff role to use this command!", ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"🎯 Quest: {title}",
        description=content,
        color=0xff9900
    )
    embed.add_field(name="Reward", value="50 XP", inline=True)
    embed.add_field(name="Complete", value="React with ✅", inline=True)
    embed.set_footer(text="React with ✅ to mark this quest as complete!")
    
    # Send to quest channel if set, otherwise current channel
    channel_id = quest_bot.quest_channel_id
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel and hasattr(channel, 'send'):
            quest_message = await channel.send(embed=embed)
        else:
            quest_message = await interaction.followup.send(embed=embed)
    else:
        await interaction.response.send_message(embed=embed)
        quest_message = await interaction.original_response()
    
    # Add checkmark reaction
    await quest_message.add_reaction('✅')
    
    # Ping quest role - first check manual setting, then auto-find @Quests role
    quest_role = None
    if quest_bot.quest_ping_role_id:
        quest_role = interaction.guild.get_role(quest_bot.quest_ping_role_id)
    
    if not quest_role:
        # Auto-find @Quests role
        quest_role = discord.utils.get(interaction.guild.roles, name="Quests")
    
    if quest_role:
        ping_msg = await quest_message.channel.send(f"{quest_role.mention} New quest available!")
        await asyncio.sleep(2)
        await ping_msg.delete()
    
    # Save quest to database
    if quest_bot.db_connection:
        cursor = quest_bot.db_connection.cursor()
        cursor.execute('INSERT INTO quests (message_id, guild_id, channel_id, title, content) VALUES (?, ?, ?, ?, ?)',
                      (quest_message.id, interaction.guild.id, quest_message.channel.id, title, content))
        quest_bot.db_connection.commit()
    
    if not channel_id or not channel or not hasattr(channel, 'send'):
        await interaction.response.send_message("✅ Quest created!", ephemeral=True)

@bot.tree.command(name="removequest", description="Remove a quest by message ID")
@app_commands.describe(message_id="ID of the quest message to remove")
async def slash_remove_quest(interaction: discord.Interaction, message_id: str):
    # Check if user has staff role
    staff_roles = ['staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN']
    if not any(role.name in staff_roles for role in interaction.user.roles):
        await interaction.response.send_message("❌ You need the @staff role to use this command!", ephemeral=True)
        return
    
    try:
        msg_id = int(message_id)
        # Remove from database
        if quest_bot.db_connection:
            cursor = quest_bot.db_connection.cursor()
            cursor.execute('DELETE FROM quests WHERE message_id = ?', (msg_id,))
            quest_bot.db_connection.commit()
        
        # Try to delete the message
        try:
            message = await interaction.channel.fetch_message(msg_id)
            await message.delete()
        except:
            pass
        
        await interaction.response.send_message("✅ Quest removed successfully!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message("❌ Failed to remove quest. Make sure the message ID is correct.", ephemeral=True)

@bot.tree.command(name="questping", description="Set the role to ping for new quests")
@app_commands.describe(role="Role to ping for quests")
async def slash_set_quest_ping(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need Manage Roles permission to use this command!", ephemeral=True)
        return
    
    quest_bot.quest_ping_role_id = role.id
    quest_bot.save_settings(interaction.guild.id)
    await interaction.response.send_message(f"✅ Quest ping role set to: {role.mention}", ephemeral=True)

@bot.tree.command(name="questchannel", description="Set the channel for quest embeds")
@app_commands.describe(channel="Channel for quest embeds")
async def slash_set_quest_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ You need Manage Channels permission to use this command!", ephemeral=True)
        return
    
    quest_bot.quest_channel_id = channel.id
    quest_bot.save_settings(interaction.guild.id)
    await interaction.response.send_message(f"✅ Quest channel set to: {channel.mention}", ephemeral=True)

@bot.tree.command(name="addxp", description="Add XP to a member")
@app_commands.describe(member="Member to add XP to", amount="Amount of XP to add")
async def slash_add_xp(interaction: discord.Interaction, member: discord.Member, amount: int):
    # Check if user has staff role
    staff_roles = ['staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN']
    if not any(role.name in staff_roles for role in interaction.user.roles):
        await interaction.response.send_message("❌ You need the @staff role to use this command!", ephemeral=True)
        return
    
    new_xp, new_level = quest_bot.update_user_xp(member.id, interaction.guild.id, amount)
    
    # Get total XP including role bonuses (same as leaderboard calculation)
    total_xp = quest_bot.calculate_total_user_xp(member.id, interaction.guild.id)
    total_level = quest_bot.calculate_level(total_xp)
    
    embed = discord.Embed(
        title="XP Added",
        description=f"Added {amount} XP to {member.mention}\nNew Total: {total_xp:,} XP (Level {total_level})",
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="removexp", description="Remove XP from a member")
@app_commands.describe(member="Member to remove XP from", amount="Amount of XP to remove")
async def slash_remove_xp(interaction: discord.Interaction, member: discord.Member, amount: int):
    # Check if user has staff role
    staff_roles = ['staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN']
    if not any(role.name in staff_roles for role in interaction.user.roles):
        await interaction.response.send_message("❌ You need the @staff role to use this command!", ephemeral=True)
        return
    
    new_xp, new_level = quest_bot.update_user_xp(member.id, interaction.guild.id, -amount)
    
    # Get total XP including role bonuses (same as leaderboard calculation)
    total_xp = quest_bot.calculate_total_user_xp(member.id, interaction.guild.id)
    total_level = quest_bot.calculate_level(total_xp)
    
    embed = discord.Embed(
        title="XP Removed",
        description=f"Removed {amount} XP from {member.mention}\nNew Total: {total_xp:,} XP (Level {total_level})",
        color=0xff0000
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setxp", description="Set a member's XP to a specific amount")
@app_commands.describe(member="Member to set XP for", amount="Amount of XP to set")
async def slash_set_xp(interaction: discord.Interaction, member: discord.Member, amount: int):
    # Check if user has staff role
    staff_roles = ['staff', 'Staff', 'STAFF', 'admin', 'Admin', 'ADMIN']
    if not any(role.name in staff_roles for role in interaction.user.roles):
        await interaction.response.send_message("❌ You need the @staff role to use this command!", ephemeral=True)
        return
    
    # Enforce 5 XP increments
    if amount % 5 != 0:
        await interaction.response.send_message(
            "❌ **Invalid XP Amount**\nXP must be set in increments of 5.\nTry: 0, 5, 10, 15, 20, 25, 50, etc.",
            ephemeral=True
        )
        return
    
    # Ensure amount is not negative
    if amount < 0:
        await interaction.response.send_message(
            "❌ **Invalid XP Amount**\nXP cannot be set to a negative value.\nMinimum: 0 XP",
            ephemeral=True
        )
        return
    
    # Set XP directly by calculating the difference from current XP
    current_data = quest_bot.get_user_data(member.id, interaction.guild.id)
    current_xp = current_data['xp']
    xp_difference = amount - current_xp
    
    new_xp, new_level = quest_bot.update_user_xp(member.id, interaction.guild.id, xp_difference)
    
    # Get total XP including role bonuses (same as leaderboard calculation)
    total_xp = quest_bot.calculate_total_user_xp(member.id, interaction.guild.id)
    total_level = quest_bot.calculate_level(total_xp)
    
    embed = discord.Embed(
        title="XP Set",
        description=f"Set {member.mention}'s base XP to {amount}\nTotal XP: {total_xp:,} (Level {total_level})",
        color=0x0099ff
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="assignbadgexp", description="Assign XP value to badge roles - auto-detects or specify role")
@app_commands.describe(
    xp_amount="XP amount for each badge role",
    role="Optional: Specific role to assign XP to (if not provided, auto-detects all badge roles)"
)
async def slash_assign_badge_xp(interaction: discord.Interaction, xp_amount: int, role: discord.Role = None):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need Manage Roles permission to use this command!", ephemeral=True)
        return
    
    guild_id = interaction.guild.id
    if guild_id not in quest_bot.role_xp_assignments:
        quest_bot.role_xp_assignments[guild_id] = {}
    
    # If role is provided, use that; otherwise auto-detect badge roles
    if role:
        badge_roles = [role]
        detection_mode = "manual"
    else:
        # Auto-detect badge roles
        badge_roles = []
        for guild_role in interaction.guild.roles:
            if "badge" in guild_role.name.lower():
                badge_roles.append(guild_role)
        detection_mode = "auto"
    
    if not badge_roles:
        if detection_mode == "auto":
            await interaction.response.send_message("❌ **No Badge Roles Found**\nNo roles with 'badge' in the name were found in this server.\n\n**Manual Selection:** Use the `role` parameter to specify a role.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ **No Role Specified**\nNo valid role was provided for XP assignment.", ephemeral=True)
        return
    
    # List found badge roles and assign XP
    role_assignments = quest_bot.role_xp_assignments[guild_id]
    assigned_count = 0
    role_list = ""
    
    for target_role in badge_roles:
        role_id_str = str(target_role.id)
        
        # Check if already assigned
        if role_id_str not in role_assignments:
            quest_bot.role_xp_assignments[guild_id][role_id_str] = xp_amount
            assigned_count += 1
            role_list += f"• **{target_role.name}** - {xp_amount} XP\n"
        else:
            current_xp = role_assignments[role_id_str]
            role_list += f"• **{target_role.name}** - Already assigned {current_xp} XP (skipped)\n"
    
    quest_bot.save_settings(guild_id)
    
    mode_text = "auto-detected" if detection_mode == "auto" else "specified"
    embed = discord.Embed(
        title="🏅 Badge Role XP Assignment",
        description=f"Found {len(badge_roles)} {mode_text} badge role(s). Assigned XP to {assigned_count} new role(s):",
        color=0x00ff00
    )
    
    if role_list:
        embed.add_field(name="Badge Roles", value=role_list[:1024], inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="assignstreakxp", description="Assign XP value to streak roles - auto-detects or specify role")
@app_commands.describe(
    xp_amount="XP amount for each streak role",
    role="Optional: Specific role to assign XP to (if not provided, auto-detects all streak roles)"
)
async def slash_assign_streak_xp(interaction: discord.Interaction, xp_amount: int, role: discord.Role = None):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need Manage Roles permission to use this command!", ephemeral=True)
        return
    
    guild_id = interaction.guild.id
    if guild_id not in quest_bot.role_xp_assignments:
        quest_bot.role_xp_assignments[guild_id] = {}
    
    # If role is provided, use that; otherwise auto-detect streak roles
    if role:
        streak_roles = [role]
        detection_mode = "manual"
    else:
        # Auto-detect streak roles
        streak_roles = []
        for guild_role in interaction.guild.roles:
            if "streak" in guild_role.name.lower():
                streak_roles.append(guild_role)
        detection_mode = "auto"
    
    if not streak_roles:
        if detection_mode == "auto":
            await interaction.response.send_message("❌ **No Streak Roles Found**\nNo roles with 'streak' in the name were found in this server.\n\n**Manual Selection:** Use the `role` parameter to specify a role.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ **No Role Specified**\nNo valid role was provided for XP assignment.", ephemeral=True)
        return
    
    # List found streak roles and assign XP
    role_assignments = quest_bot.role_xp_assignments[guild_id]
    assigned_count = 0
    role_list = ""
    
    for target_role in streak_roles:
        role_id_str = str(target_role.id)
        
        # Check if already assigned
        if role_id_str not in role_assignments:
            quest_bot.role_xp_assignments[guild_id][role_id_str] = xp_amount
            assigned_count += 1
            role_list += f"• **{target_role.name}** - {xp_amount} XP\n"
        else:
            current_xp = role_assignments[role_id_str]
            role_list += f"• **{target_role.name}** - Already assigned {current_xp} XP (skipped)\n"
    
    quest_bot.save_settings(guild_id)
    
    mode_text = "auto-detected" if detection_mode == "auto" else "specified"
    embed = discord.Embed(
        title="🔥 Streak Role XP Assignment",
        description=f"Found {len(streak_roles)} {mode_text} streak role(s). Assigned XP to {assigned_count} new role(s):",
        color=0x00ff00
    )
    
    if role_list:
        embed.add_field(name="Streak Roles", value=role_list[:1024], inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Display the XP leaderboard")
async def slash_leaderboard(interaction: discord.Interaction):
    try:
        leaderboard_data = quest_bot.get_leaderboard(interaction.guild.id, 10)
        print(f"Slash leaderboard data retrieved: {leaderboard_data}")  # Debug print
        
        if not leaderboard_data:
            embed = discord.Embed(
                title="🏆 XP Leaderboard",
                description="No users with XP found yet!\nComplete some quests to get on the leaderboard!",
                color=0xffd700
            )
            # Still show level requirements
            level_info = "**Level Requirements:**\n"
            for level, xp in LEVEL_THRESHOLDS.items():
                level_info += f"Level {level}: {xp:,} XP\n"
            embed.add_field(name="Level System", value=level_info, inline=False)
            await interaction.response.send_message(embed=embed)
            return
        
        embed = discord.Embed(
            title="🏆 XP Leaderboard",
            description="Top 10 Quest Completers",
            color=0xffd700
        )
        
        medals = ["🥇", "🥈", "🥉"]
        users_added = 0
        
        for i, (user_id, xp, level) in enumerate(leaderboard_data):
            medal = medals[i] if i < 3 else f"#{i+1}"
            
            # Try multiple methods to get user info
            user = interaction.guild.get_member(user_id)
            if not user:
                user = bot.get_user(user_id)
            
            # Calculate total XP including role-based XP
            total_xp = quest_bot.calculate_total_user_xp(user_id, interaction.guild.id)
            
            if user:
                # Format username without pinging - use @ but escape it
                username = f"@{user.name}"
                display_name = getattr(user, 'display_name', user.name)
                if display_name != user.name:
                    username = f"@{user.name} ({display_name})"
                
                embed.add_field(
                    name=f"{medal} Level {level}",
                    value=f"{username}\n{total_xp:,} XP",
                    inline=True
                )
                users_added += 1
            else:
                # Try to fetch user info from Discord API
                try:
                    user = await bot.fetch_user(user_id)
                    username = f"@{user.name}"
                    embed.add_field(
                        name=f"{medal} Level {level}",
                        value=f"{username}\n{total_xp:,} XP",
                        inline=True
                    )
                    users_added += 1
                except:
                    # Last resort - show user ID
                    embed.add_field(
                        name=f"{medal} Level {level}",
                        value=f"@User{str(user_id)[-4:]}\n{total_xp:,} XP",
                        inline=True
                    )
                    users_added += 1
        
        if users_added == 0:
            embed.add_field(
                name="No Active Users", 
                value="Users with XP may have left the server", 
                inline=False
            )
        
        # Add level requirements info
        level_info = "**Level Requirements:**\n"
        for level, xp_req in LEVEL_THRESHOLDS.items():
            level_info += f"Level {level}: {xp_req:,} XP\n"
        
        embed.add_field(name="Level System", value=level_info, inline=False)
        await interaction.response.send_message(embed=embed)
        
    except Exception as e:
        print(f"Error in slash leaderboard command: {e}")
        await interaction.response.send_message("❌ Could not retrieve leaderboard data. Please try again later.", ephemeral=True)

@bot.tree.command(name="createlevelroles", description="Manually create all level roles (Level 1-10)")
async def slash_create_level_roles(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need Manage Roles permission to use this command!", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    await quest_bot.create_level_roles(interaction.guild)
    await interaction.followup.send("✅ Level roles created/verified for Levels 1-10!", ephemeral=True)

@bot.tree.command(name="assignlevelroles", description="Assign level roles to all users based on their current XP")
async def slash_assign_level_roles(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need Manage Roles permission to use this command!", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Get all users from database
    leaderboard_data = quest_bot.get_leaderboard(interaction.guild.id, 1000)  # Get all users
    
    assigned_count = 0
    for user_id, xp, level in leaderboard_data:
        member = interaction.guild.get_member(user_id)
        if member:
            # Remove any existing level roles
            level_roles = [role for role in member.roles if role.name.startswith("Level ")]
            if level_roles:
                await member.remove_roles(*level_roles, reason="Reassigning level roles")
            
            # Add correct level role
            level_role_name = f"Level {level}"
            level_role = discord.utils.get(interaction.guild.roles, name=level_role_name)
            if level_role:
                await member.add_roles(level_role, reason=f"Assigned {level_role_name} based on XP")
                assigned_count += 1
    
    await interaction.followup.send(f"✅ Assigned level roles to {assigned_count} users based on their current XP!", ephemeral=True)

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!", delete_after=5)
    elif isinstance(error, commands.MissingRole):
        await ctx.send("❌ You need the @staff role to use this command!", delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument provided!", delete_after=5)
    else:
        await ctx.send("❌ An error occurred while processing the command!", delete_after=5)

if __name__ == "__main__":
    import os
    
    # Get token from environment variable
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        print("Error: DISCORD_BOT_TOKEN environment variable not set!")
        print("Please set your Discord bot token as an environment variable.")
        exit(1)
    
    # Run the bot
    bot.run(TOKEN)
