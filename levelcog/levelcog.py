from redbot.core import commands
import asyncio
from re import split
import urllib.request
import requests # Used to validate urls (that are sent in by users through commands)
import discord
from discord import user
from discord.ext import tasks
from discord.ext.commands.errors import CommandInvokeError, MemberNotFound
from discord.utils import get
from datetime import timedelta
from datetime import datetime
from datetime import time
import pymongo
from pymongo import MongoClient
import json
import calendar # calendar libraray is used for quick conversion from int (returned by weekday()) to weekday str.
import distutils.util
from PIL import Image, ImageDraw, ImageFont
from discord.utils import get


class Levelcog(commands.Cog):   

    def __init__(self, bot):
        self.bot : commands.Bot = bot
        self.db = None
        self.client = None
        self.collection = None
        self.connected = False # Whether the bot is connected to the db.
        json_dict = json.load(open('data.json', 'r'))
        self.xp_per_message = json_dict['xp_per_message']
        self.voice_xp_rate = json_dict['voice_xp_rate']
        self.bonus_xp_rate = json_dict['bonus_xp_rate']
        self.bonus_xp_days = json_dict['bonus_days']
        self.solo_get_xp = json_dict['solo_xp']
        self.muted_get_xp = json_dict['muted_xp']
        self.deafened_get_xp = json_dict['deafened_xp']
        self.levelfactor = json_dict['level_factor']
        self.daily_leaderboard_channel = str(json_dict['daily_leaderboard_channel'])
        self.monthly_leaderboard_channel = str(json_dict['monthly_leaderboard_channel'])
        self.valid_days = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday']
        self.connect_to_db()

    def connect_to_db(self):
        try:
            self.client = MongoClient('mongodb://localhost:27017/?readPreference=primary&appname=MongoDB%20Compass&directConnection=true&ssl=false')
            self.db = self.client.discord_members
            self.collection = self.db.members
            self.connected = True
            return True # Connection successfully made, return true
        except(Exception):
            print("Connection to db failed.")
            self.connected = False
            return False # Connection could not be made, return false

    def determine_level(self, xp): # Formula to get the level a user should have according to the amount of xp they have.
        level = 1
        totalpointreq = 250
        currentpointreq = 250
        pointreqchange = self.levelfactor # levelfactor is 150 by default.
        if xp > 100:
            while xp >= totalpointreq:
                level += 1
                currentpointreq += pointreqchange
                totalpointreq += currentpointreq
        return level

    def determine_xp(self, level): # Formula to get the amount of xp a user should have according to the level they have.
        lev = 1
        totalpointreq = 0
        currentpointreq = 100
        pointreqchange = self.levelfactor # levelfactor is 150 by default.
        if level > 1:
            while level > lev:
                currentpointreq += pointreqchange
                totalpointreq += currentpointreq
                lev += 1
        return totalpointreq

    def register_user(self, user): # Register user into the database
        if not self.connected and not self.connect_to_db(): return False # If a connection to database hasn't been established then we try to make connection here. If we can't then we return false and exit the function    
        try:    
            self.collection.insert_one({
                        'name' : user.name,
                        '_id' : user.id, # _id (which is the primary key/identifier/unique field among all member entries in the database) is the discord user's id.
                        'level' : 1,
                        'normal_xp' : 0,
                        'bonus_xp' : 0,
                        'total_xp' : 0,
                        'voice_xp' : 0, 
                        'time_spent_in_vc' : 0, # Note: time_spent_in_vc is in minutes
                        'messages_sent' : 0, # Amount of messages sent by the user within the server (ones only tracked when the bot is online)
                        'daily_messages_sent' : 0, # Amount of messages sent in the current day
                        'monthly_messages_sent' : 0, # Amount of messages sent in the current month
                        'background' : None 
            })
            return True # Could register user into the db    

        except(Exception): return False # Exception occurred and so we can't add the user.

    def update_user_in_db(self, user): # Updates the level field of a user, in the db, accordingly with the users total xp. Called whenever the normal_xp or bonus_xp of a user is changed.
        try:
            cloned_dict = dict(self.collection.find_one({'_id' : user.id}))
            self.collection.update_one({'_id' : user.id}, {'$set' : {'total_xp' : cloned_dict['normal_xp'] + cloned_dict['bonus_xp'] + cloned_dict['voice_xp']}}) # Updates the users total_xp field, within the db, which is calculated by adding normal_xp and bonus_xp.
            cloned_dict = dict(self.collection.find_one({'_id' : user.id})) # Refreshing the the dict to have the newly updated xp value
            self.collection.update_one({'_id' : user.id}, {'$set' : {'level' : self.determine_level(cloned_dict['total_xp'])}}) # Then it updates the users level field, in the db, accordingly with the users total_xp.
        except(Exception):
            self.register_user(user) # Registers user if they weren't in DB.
        
    def check_perms(self, user : discord.Member): # Takes in a user and checks and returns whether they have server admin perms
        return user.guild_permissions.administrator # Used for all admin-level commands.

    @tasks.loop(minutes=1)
    async def check_month(self): # Checks the month, if it has changed, then it updates the monthly leaderboard.
        json_data = json.load(open('data.json', 'r'))
        month = json_data['month']
        if month != datetime.now().strftime('%B').lower(): # If the month has changed
            await self.update_monthly_leaderboard()
        json_data['month'] = datetime.now().strftime('%B').lower()
        with open('data.json', 'w') as f:
            json.dump(json_data, f, indent=4)

    @tasks.loop(minutes=1)
    async def give_voice_xp(self): # Voice xp is given per minute, so this function is called every minute to check every single voice channel in the server and give users voice xp accordingly.
        if self.connected:
            for channel in self.bot.guilds[0].voice_channels: # We can just use bot.guilds[0] because the bot is only in 1 server.
                json_dict = json.load(open('data.json', 'r'))
                non_bot_members = [] # Non-bot users connected to the voice channel.
                for m in channel.members: 
                    if not m.bot: non_bot_members.append(m)
                if len(non_bot_members) == 1 and not self.solo_get_xp: continue # We skip the current channel if it only has 1 user connected to it and members, that are alone in vc, currently shouldn't get xp.
                if len(non_bot_members) > 0 and channel.id not in json_dict['blacklisted']['channels'] and channel.category.id not in json_dict['blacklisted']['channels']:
                    for member in non_bot_members:
                        if not self.muted_get_xp and member.voice.self_mute: continue # We skip/don't give the current member xp if they're muted and muted members currently shouldn't get xp.
                        if not self.deafened_get_xp and member.voice.self_deaf: continue # We skip/don't give the current member xp if they're deafened and deafened members currently shouldn't get xp.
                        if not member.bot and self.collection.find_one({'_id' : member.id}) != None:
                            self.collection.update_one({'_id' : member.id}, {'$inc' : {'voice_xp' : self.voice_xp_rate, 'time_spent_in_vc' : 1}})
                            self.update_user_in_db(member)

    async def reset_daily_messages(self):
        if self.connected:
            self.collection.update_many({}, {'$set' : {'daily_messages_sent' :  0}})

    async def reset_monthly_messages(self):
        if self.connected:
            self.collection.update_many({}, {'$set' : {'monthly_messages_sent' : 0}})
        current_time : datetime = datetime.now()
        #last_day = current_time + timedelta(days=(calendar.monthrange(current_time.year, current_time.month)[1] - current_time.day))  
        #self.bot.loop.create_task((self.reset_monthly_messages((datetime.combine(last_day, time.min) - current_time).total_seconds())))

    @commands.Cog.listener()
    async def on_ready(self): 
        self.give_voice_xp.start()
        self.check_month.start()
        current_time = datetime.now()
        tomorrow = current_time + timedelta(days=1)
        self.bot.loop.create_task(self.start_daily_leaderboard_update((datetime.combine(tomorrow, time.min) - current_time).total_seconds())) # Calculates the amount of seconds until the day ends (so time until 12AM), then schedules the start, of the dailyleaderboard channel update cycle, to take place after that amount of time has passed. 

    async def start_daily_leaderboard_update(self, delay): # Starts the daily update_daily_leaderboard() update cycle after a specified delay, which is usually 24 hours
        await asyncio.sleep(delay)
        self.update_daily_leaderboard.start()

    @commands.Cog.listener()
    async def on_message(self, message : discord.Message):
        author = message.author
        if author.bot: return
        self.update_user_in_db(author) # Checks and registers users 
        if self.connected: 
            self.collection.update_one({'_id' : author.id}, {'$inc' : {'messages_sent' : 1, 'daily_messages_sent' : 1, 'monthly_messages_sent' : 1}}) # Increasing the messages_sent, daily_messages_sent and monthly_messages_sent field by 1 because they have to be increased no matter what condition (except for whether the bot is connected to the database of course).
            entry = self.collection.find_one({'_id' : author.id}) 
            json_data = json.load(open('data.json', 'r'))
            if str(author.id) in json_data['last_messages']:
                time_diff = datetime.now() - datetime.fromisoformat(json_data['last_messages'][str(author.id)]) # The difference in time/time passed between the last message the user sent and the message they just sent.
            else: time_diff = timedelta(seconds=11) # Just setting time_diff to a timedelta which is 11 seconds if the bot doesn't have the time (at which the user's last message was sent) stored in the json. This is done to make sure the user gets the xp, as seen below.
            old_user_level = self.collection.find_one({'_id' : author.id})['level']
            json_data['last_messages'][str(author.id)] =  str(datetime.now()) # Updating the last_message entry in the json.
            if entry != None and message.channel.id not in json_data['blacklisted']['channels'] and message.channel.category_id not in json_data['blacklisted']['categories'] and time_diff >= timedelta(seconds=10): # If the entry could be extracted from the database and if the channel or category the message was sent isn't blacklisted.
                self.collection.update_one({'_id' : author.id}, {'$inc' : {'normal_xp' : self.xp_per_message}, '$set' : {'level' : self.determine_level(entry['normal_xp'] + self.xp_per_message)}}) # Updating the normal xp and level fields in the database.
                self.update_user_in_db(author)
                if calendar.day_name[datetime.now().weekday()].lower in json_data['bonus_days']: # If today is one of the bonus xp days:
                    self.collection.update_one({'_id' : author.id}, {'$inc' : {'bonus_xp' : self.xp_per_message * self.bonus_xp_rate}}) # Updating the bonus_xp field in the database.
                    self.update_user_in_db(author)
            user_info = dict(self.collection.find_one({'_id' : author.id}))      
            new_user_level = user_info['level']
            if new_user_level > old_user_level: # If user's level increased, we send a message.
                channel = get(message.guild.channels, id=json_data['level_up_channel'])
                if channel == None:
                    return
                embed = discord.Embed(title='You leveled up!', description=f"Congratulations {author.mention}! You've leveled up to {new_user_level}.")
                embed.set_thumbnail(url=author.avatar_url)
                embed.set_footer(text=datetime.now().strftime('%b %d, %Y %I:%M %p'))
                for role in json_data['roles']: # Iterating through all the level-binded roles to see if anyone of them are associated to the level the user just reached. 
                    if role['level-required'] == new_user_level:  
                        await author.add_roles(get(message.guild.roles, name=role['name']))
                        user_roles = [] 
                        for f in author.roles:
                            user_roles.append(f.name)
                        for r in json_data['roles']: # Removing the previous level roles from the user.
                            if r['name'] in user_roles and r['level-required'] != new_user_level:
                                await author.remove_roles(get(message.guild.roles, name=r['name']))
                        embed.add_field(name='New Level Role', value=f":trophy: {role['name']}")
                        embed.add_field(name='Current Experience', value=f"{user_info['total_xp']}XP")
                await channel.send(embed=embed)
            with open('data.json', 'w') as file:
                json.dump(json_data, file, indent=4)
    
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction : discord.Reaction, user):
        if user.bot: return
        message : discord.Message = reaction.message
        message_id = str(message.id)
        json_data = json.load(open('data.json', 'r'))
        if message_id in json_data['embed_data'] and "Leaderboard" in message.embeds[0].title:
            try:
                embed_data : list = json_data['embed_data'][message_id]
            except(KeyError):
                await message.remove_reaction(reaction.emoji, user)
                return
            i = -1
            for embed in embed_data: #  Iterate through all the embeds in the leaderboard embed list and return the index of the embed that is currently being shown in the current leaderboard message.
                print(embed['title']) 
                print(message.embeds[0].title)
                if embed['title'] == message.embeds[0].title: # If the message that was reacted to is the message that currently/supposedly is the message thats supposed to have the leaderboard.
                    i = embed_data.index(embed)
                    break

            to_embed = message.embeds[0]
            print(reaction.emoji == '▶')
            print(i != len(embed_data) - 1)
            if reaction.emoji == '▶' and i != len(embed_data) - 1: 
                to_embed.description = embed_data[i + 1]['description']
                to_embed.title = embed_data[i + 1]['title']
                
                await message.edit(embed=to_embed)
            elif reaction.emoji == '◀' and i != 0: 
                to_embed.description = embed_data[i - 1]['description']
                to_embed.title = embed_data[i - 1]['title']
                await message.edit(embed=to_embed)
            
            await message.remove_reaction(reaction.emoji, user)

    @commands.command()
    async def status(self, ctx, *args): # Returns embed containing the bot's status, like its connection to the database, latency etc.
        if not self.check_perms(ctx.author): return
        embed = discord.Embed(title='Bot Status')
        embed.set_footer(text=str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")))
        embed.add_field(name='Connection to database', value=self.connected, inline=True)
        embed.add_field(name='Latency', value=self.bot.latency, inline=True)
        embed.set_thumbnail(url=self.bot.user.avatar_url)
        await ctx.send(embed=embed)

    @commands.command()
    async def initialise(self, ctx, *args):
        if not self.check_perms(ctx.author): return
        embed : discord.Embed = discord.Embed(title="Members added to the database", description='') # Embed that stores all the members added to the database.
        for m in self.bot.guilds[0].members: # Bot is only in 1 server/guild thats why we can use self.bot.guilds[0]
            if m.bot: continue
            if self.collection.find_one({'_id' : m.id}) == None: # If the member could not be found in the db, then find_one() returns NoneType, so that's how we know the member doesn't exist in the db.
                if self.connected: # Registering user into db. If they can't then that means a connection to the db couldn't be established
                    self.register_user(m)
                    embed.description += "\n " + m.mention
                else: 
                    await ctx.send('A connection to the database could not be made, as a result, members of the server, who are not in the database, could not be initialised into the database.')
                    return
        await ctx.send(embed=embed)
    
    @commands.command()
    async def add_level(self, ctx, user : discord.Member,  level_arg : int):
        if not self.check_perms(ctx.author): return
        try:
            level = int(level_arg)
            if level >= 1 and self.connected: # If connection to database is alive and level being added is more than 0
                current = dict(self.collection.find_one({'_id' : user.id})) # The current bonus xp the user has, according to the db.
                current_bonus_xp = current['bonus_xp']
                new_level = current['level'] + level
                self.collection.update_one({'_id' : user.id}, {'$inc' : {'level' : level}, '$set' : {'normal_xp' : self.determine_xp(new_level) - current_bonus_xp, 'total_xp' : self.determine_xp(level)}}) # Updating the member entry in the database according to their level.
                await ctx.send('Gave ' + str(level) + ' levels to ' + user.mention + "!")
            elif level <= 0: raise TypeError # Raising typeerror here so that it passes on to the except statement and sends the "invalid levels" message. 
            else: await ctx.send("Connection to database could not be made!") # Otherwise, the only scenario is that the bot is not connected to the database.
        except(TypeError):
            await ctx.send("Invalid amount of levels was passed in!")
        except(AttributeError): # AttributeError is thrown if the user value could not be found or initialized.
            await ctx.send('Invalid username was passed in!')

    @commands.command()
    async def subtract_level(self, ctx, user : discord.Member, level_arg : int):
        if not self.check_perms(ctx.author): return
        try:
            level = int(level_arg)
            if level >= 1 and self.connected:
                current = dict(self.collection.find_one({'_id' : user.id})) # The current bonus xp the user has, according to the db.
                current_bonus_xp = current['bonus_xp']
                new_level = current['level'] - level
                if new_level <= 0: raise TypeError # We can't minus the level all the way to to 0 or negative.
                self.collection.update_one({'_id' : user.id}, {'$inc' : {'level' : -level}, '$set' : {'normal_xp' : self.determine_xp(new_level) - current_bonus_xp, 'total_xp' : self.determine_xp(level)}})
                await ctx.send('Took away ' + str(level) + ' levels from ' + user.mention + "!")
            elif level <= 0: raise TypeError # Raising typeerror here so that it sends the invalid amount of levels message
            else: await ctx.send("Connection to database could not be made!")    
        except(TypeError):
            await ctx.send("Invalid amount of levels was passed in!")
        except(AttributeError):
            await ctx.send("Invalid username was passed in!")

    @commands.command()
    async def set_level(self, ctx, user : discord.Member, level_arg : int, *args):
        if not self.check_perms(ctx.author): return
        try:
            level = int(level_arg)
            if level >= 1 and self.connected:
                current_bonus_xp = dict(self.collection.find_one({'_id' : user.id}))['bonus_xp'] # The current bonus xp the user has, according to the db.
                self.collection.update_one({'_id': user.id}, {'$set' : {'level' : level, 'normal_xp' : self.determine_xp(level) - current_bonus_xp, 'total_xp' : self.determine_xp(level)}})
                await ctx.send('Set ' + user.mention + "'s level to " + str(level))
            elif level <= 0: 
                raise TypeError
            else: await ctx.send("Connection to database could not be made!") 
        except(TypeError):
            await ctx.send("Invalid amount of levels was passed in!")
        except(AttributeError):
            await ctx.send("Invalid username was passed in!")

    @commands.command()
    async def reset_level(self, ctx, user : discord.Member, *args):
        if not self.check_perms(ctx.author): return
        try:
            if self.connected:
                self.collection.update_one({'_id' : user.id}, {'$set' : {'level' : 1, 'normal_xp' : 0, 'bonus_xp' : 0, 'total_xp' : 0}})
                await ctx.send('Reset ' + user.mention + "'s level to 1!")
            else:
                await ctx.send("Connections to database could not be made!")
        except(AttributeError):
            await ctx.send("Invalid username was passed in!")    

    @commands.command()
    async def add_xp(self, ctx, user : discord.Member, xp_arg : int, *args):
        if not self.check_perms(ctx.author): return
        try:
            xp = int(xp_arg)
            if xp >= 1 and self.connected:
                self.collection.update_one({'_id' : user.id}, {'$inc' : {'normal_xp' : xp}}) # We increase the normal_xp field of the user, within the database, by the amount of xp passed in.
                self.update_user_in_db(user) # Then we call update_user_in_db() to update the total_xp and level fields of the user within the database.
            elif xp < 1: 
                raise TypeError
            else: await ctx.send("Connection to database could not be made!") 
        except(TypeError):
            await ctx.send("Invalid amount of xp was passed in!")
            
    @commands.command()
    async def subtract_xp(self, ctx, user : discord.Member, xp_arg : int, *args):
        if not self.check_perms(ctx.author): return
        try:
            xp = int(xp_arg)
            if xp >= 1 and self.connected:
                self.collection.update_one({'_id' : user.id}, {'$inc' : {'normal_xp' : -xp}}) # We decrease the normal_xp field of the user, within the database, by the amount of xp passed in (by using the negation of the number of xp passed in).
                self.update_user_in_db(user) # Then we call update_user_in_db() to update the total_xp and level fields of the user within the database.
            elif xp < 1: 
                raise TypeError
            else: await ctx.send("Connection to database could not be made!") 
        except(TypeError):
            await ctx.send("Invalid amount of xp was passed in!")

    @commands.command()
    async def set_bonus_xp_days(self, ctx, *args):
        if not self.check_perms(ctx.author): return
        for arg in args:
            if self.connected and arg.lower() in self.valid_days: 
                arg_lower = arg.lower()
                if arg_lower not in self.bonus_xp_days: # If the current day entered IS NOT in the already active bonus days
                    self.bonus_xp_days.append(arg_lower) # We add it
                    temp_dict = dict(json.load(open('data.json', 'r')))
                    temp_dict['bonus_days'] = self.bonus_xp_days
                    with open('data.json', 'w') as file:
                        json.dump(temp_dict, file, indent=4)
                    await ctx.send(arg + " has been set as a bonus xp day!")
                else: await ctx.send(arg + ' is already a bonus xp day!') # Otherwise it's already one of the active bonus xp days. 
            elif not self.connected: await ctx.send("The bot could not connect to the database.")
            else: await ctx.send('You sent in an invalid day!')
    
    @commands.command()
    async def unset_bonus_xp_days(self, ctx, *args):
        if not self.check_perms(ctx.author): return
        for arg in args:
            if self.connected and arg.lower() in self.valid_days:
                arg_lower = arg.lower()
                if arg_lower in self.bonus_xp_days: # If the current day entered IS in the already active bonus days
                    self.bonus_xp_days.remove(arg_lower) # We remove it.
                    temp_dict = dict(json.load(open('data.json', 'r')))
                    temp_dict['bonus_days'] = self.bonus_xp_days
                    with open('data.json', 'w') as file:
                        json.dump(temp_dict, file, indent=4)
                    await ctx.send(arg + ' is no longer a bonus xp day!')
                else: await ctx.send(arg + ' is not a bonus xp day!')
            elif not self.connected: await ctx.send("The bot could not connect to the database.")
            else: await ctx.send('You sent in an invalid day!')
        
    @commands.command()
    async def set_xp_per_message(self, ctx, xp, *args):
        if not self.check_perms(ctx.author): return
        try:
            xp = int(xp) # Trying to convert the xp arg to a number
            if xp < 0: raise ValueError # We can't have negative xp for a number
        except(ValueError): # ValueError is thrown if xp couldn't be converted to int.
            await ctx.send('You sent in an invalid number!')
            return
        self.xp_per_message = xp
        # Updates the levelfactor field in data.json, so that it's value is used as the xp/levelfactor when the bot is started up next time. 
        with open('data.json', 'r') as file: # Creates temp dict object to hold all the values
            temp_dict = json.load(file) 
        temp_dict['xp_per_message'] = xp # Updates levelfactor value in cloned dict object
        with open('data.json', 'w') as file: # Sets data.json to the newly updated cloned dict
            json.dump(temp_dict, file, indent=4)
        await ctx.send('The amount of xp given per message has been set to ' + str(xp))

    @commands.command()
    async def set_bonus_xp_rate(self, ctx, rate, *args):
        if not self.check_perms(ctx.author): return
        try:
            rate = int(rate)
            if rate < 0: raise ValueError # Rates below 0 are invalid.
        except(ValueError):
            await ctx.send('The rate you sent in is an invalid number!')
            return
        self.bonus_xp_rate = rate
        # Updates the bonus_xp_rate field in data.json
        temp_dict = json.load('data.json', 'r') 
        temp_dict['bonus_xp_rate'] = rate 
        with open('data.json', 'w') as file:
            json.dump(temp_dict, file, indent=4)
        await ctx.send('The rate of bonus xp has been set to ' + str(rate))    

    @commands.command()
    async def set_voice_xp_rate(self, ctx, rate, *args):
        if not self.check_perms(ctx.author): return
        try:
            rate = int(rate)
            if rate < 0: raise ValueError
        except(ValueError):
            await ctx.send('The rate you sent in is an invalid number!')
            return
        self.voice_xp_rate = rate
        temp_dict = json.load(open('data.json', 'r'))
        temp_dict['voice_xp_rate'] = rate
        with open('data.json', 'w') as file:
            json.dump(temp_dict, file, indent=4)
        await ctx.send('The amount of voice xp given per minute has been set ' + str(rate))

    @commands.command()
    async def set_solo_xp(self, ctx, solo, *args): # Whether users, that are alone in vc, should gain xp.
        if not self.check_perms(ctx.author): return
        try:
            solo = bool(distutils.util.strtobool(solo))
            json_dict = json.load(open('data.json', 'r'))
            json_dict['solo_xp'] = solo
            with open('data.json', 'w') as file:
                json.dump(json_dict, file, indent=4)
            self.solo_get_xp = solo
        except(ValueError):
            await ctx.send("The value you passed in was invalid! Please pass in true or false only. (Example: .set_solo_xp true)")

    @commands.command()
    async def set_muted_xp(self, ctx, muted, *args): # Whether users, that are muted in vc, should gain xp.
        if not self.check_perms(ctx.author): return
        try:
            muted = bool(distutils.util.strtobool(muted))
            json_dict = json.load(open('data.json', 'r'))
            json_dict['muted_xp'] = muted
            with open('data.json', 'w') as file:
                json.dump(json_dict, file, indent=4)
            self.muted_get_xp = muted
        except(ValueError):
            await ctx.send("The value you passed in was invalid! Please pass in true or false only. (Example: .set_muted_xp false)")

    @commands.command()
    async def set_deafened_xp(self, ctx, deaf, *args): # Whether users, that are deafened in vc, should gain xp.
        if not self.check_perms(ctx.author): return
        try:
            deaf = bool(distutils.util.strtobool(deaf))
            json_dict = json.load(open('data.json', 'r'))
            json_dict['deaf_xp'] = deaf
            with open('data.json', 'w') as file:
                json.dump(json_dict, file, indent=4)
            self.deaf_get_xp = deaf
        except(ValueError):
            await ctx.send("The value you passed in was invalid! Please pass in true or false only. (Example: .set_deaf_xp true)")

    @commands.command()
    async def blacklist_channel(self, ctx, channel, *args):
        if not self.check_perms(ctx.author): return
        try:
            channel = self.bot.get_channel(int(channel.replace('<', '').replace('>', '').replace('#', '')))
        except(Exception): # if there was an error while trying to get the channel
            await ctx.send('The channel you sent in could not be found!')
            return
        temp_dict = json.load(open('data.json', 'r'))
        if channel.id not in temp_dict['blacklisted']['channels']:
            temp_dict['blacklisted']['channels'].append(channel.id)
        else:
            await ctx.send('The channel you sent in is already blacklisted!')
            return
        with open('data.json', 'w') as file:
            json.dump(temp_dict, file, indent=4)
        await ctx.send(channel.mention + ' is now blacklisted. None of the messages sent in it will gain xp for their sender!')

    @commands.command()
    async def unblacklist_channel(self, ctx, channel, *args):
        if not self.check_perms(ctx.author): return
        try:
            channel = self.bot.get_channel(int(channel.replace('<', '').replace('>', '').replace('#', '')))
        except(Exception): # if there was an error while trying to get the channel
            await ctx.send('The channel you sent in could not be found!')
            return
        temp_dict = json.load(open('data.json', 'r'))
        if channel.id in temp_dict['blacklisted']['channels']: # Only remove it if it is in the blacklisted channels list
            temp_dict['blacklisted']['channels'].remove(channel.id)
        else:
            await ctx.send('The channel you sent in is already not blacklisted!')
            return 
        with open('data.json', 'w') as file:
            json.dump(temp_dict, file, indent=4)
        await ctx.send(channel.mention + ' is no longer blacklisted. Messages sent in it will now gain xp for their sender!')

    @commands.command()
    async def blacklist_category(self, ctx, *args):
        if not self.check_perms(ctx.author): return
        try:
            category_id = int(args[0]) # Trying to convert arg into int by default, presuming its an int for ID
            category = get(ctx.guild.categories, id=category_id)
        except(ValueError): # if there was an error in trying to convert it, then it must be the category name that is being passed in=
            category_name = ''
            for arg in args:
                category_name += arg + ' '
            category = get(ctx.guild.categories, name=category_name[:-1]) # :-1 to remove the last extra space
        if category == None: # If category couldn't be found
            await ctx.send('The category you sent in could not be found!')
        else:
            temp_dict = json.load(open('data.json', 'r'))
            if category.id not in temp_dict['blacklisted']['categories']:
                temp_dict['blacklisted']['categories'].append(category.id)
            else: 
                await ctx.send('The category you sent in is already blacklisted!')
                return
            with open('data.json', 'w') as file:
                json.dump(temp_dict, file, indent=4)
            await ctx.send(category.mention + ' is now blacklisted. None of the messages sent in it will gain xp for their sender!')

    @commands.command()
    async def unblacklist_category(self, ctx, *args):
        if not self.check_perms(ctx.author): return
        try:
            category_id = int(args[0]) # Trying to convert arg into int by default, presuming its an int for ID
            category = get(ctx.guild.categories, id=category_id)
        except(ValueError): # if there was an error in trying to convert it, then it must be the category name that is being passed in=
            category_name = ''
            for arg in args:
                category_name += arg + ' '
            category = get(ctx.guild.categories, name=category_name[:-1]) # :-1 to remove the last extra space
        if category == None: # If category couldn't be found
            await ctx.send('The category you sent in could not be found!')
        else:
            temp_dict = json.load(open('data.json', 'r'))
            if category.id in temp_dict['blacklisted']['categories']: # Only remove it if it is in the blacklisted categories list
                temp_dict['blacklisted']['categories'].remove(category.id)
            else:
                await ctx.send('The category you sent in is already not blacklisted!')
                return
            with open('data.json', 'w') as file:
                json.dump(temp_dict, file, indent=4)
            await ctx.send(category.mention + ' is no longer blacklisted. Messages sent in it will now gain xp for their sender!')
    
    @commands.command()
    async def leaderboard(self, ctx):
        if self.connected:
            msg_sent : discord.Message = await ctx.send("Fetching member data...")
            leaderboard = list(self.collection.find({}).sort('total_xp', pymongo.DESCENDING))
            total_count = len(leaderboard)
            embed_list = []
            i = 0
            first_current_datetime = datetime.now()
            while i < total_count - 1: # Keep making pages as long as there are user entries left.
                embed = discord.Embed(title='Leaderboard ' + "(Showing " + str(i + 1) + " - " + str(total_count) + ")", description='')
                current_embed_amount = 0
                to_desc = ""
                while (current_embed_amount < 10 and i < total_count - 1):
                    
                    user_dict = self.collection.find_one({'_id' : leaderboard[i]['_id']})
                    
                    user_dict['invites_sent'] = 0
                    for invite in await ctx.guild.invites():
                        if invite.inviter.id == user_dict['_id']:
                            user_dict['invites_sent'] += 1
                    to_desc += str(i + 1) + '. ' + self.bot.get_user(leaderboard[i]['_id']).name + '  :military_medal: ' + str(user_dict['level']) + '\n' + str(user_dict['total_xp']) + " XP ⬄ :writing_hand:" + str(user_dict['messages_sent']) + ":black_small_square::microphone2:" + str(round(user_dict['time_spent_in_vc']/60, 1)) + ":black_small_square::envelope:" + str(user_dict['invites_sent']) + ":black_small_square::trophy:" + str(user_dict['bonus_xp'])  + '\n'
                    i += 1
                    current_embed_amount += 1
                embed.description = to_desc
                embed.title = embed.title.split('-')[0] + ' - ' + str(embed.description.split('\n')[-3].split('.')[0]) + ')'
                embed.set_footer(text = str(first_current_datetime.strftime("%d/%m/%Y %H:%M:%S")))
                embed.set_thumbnail(url=ctx.guild.icon_url)
                embed_list.append(embed)

            if len(embed_list) > 0:
                json_data = json.load(open('data.json', 'r'))
                embed_to_json_data = []
                for e in embed_list: 
                    embed_to_json_data.append({
                        'title' : e.title,
                        'description' : e.description
                    })
                json_data['embed_data'][msg_sent.id] = embed_to_json_data
                with open('data.json', 'w') as f:
                    json.dump(json_data, f, indent=4)
                await msg_sent.edit(content=None, embed=embed_list[0])
                await msg_sent.add_reaction("\U000025c0")
                await msg_sent.add_reaction("\U000025b6")
    
    @commands.command(name='daily_leaderboard')
    async def daily_leaderboard(self, ctx, *args):
        if self.connected:
            leaderboard = list(self.collection.find({}).sort('daily_messages_sent', pymongo.DESCENDING))
            total_count = len(leaderboard)
            embed_list = []
            first_current_datetime = datetime.now()
            msg_sent = await ctx.send('Gathering data...')
            i = 0
            while i < total_count - 1: # Keep making pages as long as there are user entries left, just like the normal/default leaderboard.
                embed = discord.Embed(title=f'Daily Leaderboard (Showing {i + 1} - {total_count})', description='')
                current_embed_amount = 0
                to_desc = ""
                while (current_embed_amount < 10 and i < total_count - 1):
                    to_desc += str(i+1) + '. ' + self.bot.get_user(leaderboard[i]['_id']).name + ':black_small_square::writing_hand:' + str(leaderboard[i]['daily_messages_sent']) + '\n'
                    i += 1
                    current_embed_amount += 1
                embed.description = to_desc
                embed.title = embed.title.split('-')[0] + ' - ' + str(embed.description.split('\n')[-2].split('.')[0]) + ')'
                embed.set_footer(text = str(first_current_datetime.strftime("%d/%m/%Y %H:%M:%S")))
                embed.set_thumbnail(url=ctx.guild.icon_url)
                embed_list.append(embed)

            if len(embed_list) > 0:
                json_data = json.load(open('data.json', 'r'))
                embed_to_json_data = []
                for e in embed_list: 
                    embed_to_json_data.append({
                        'title' : e.title,
                        'description' : e.description
                    })
                
                json_data['embed_data'][msg_sent.id] = embed_to_json_data
                with open('data.json', 'w') as f:
                    json.dump(json_data, f, indent=4)
                await msg_sent.edit(content=None, embed=embed_list[0])
                await msg_sent.add_reaction("\U000025c0")
                await msg_sent.add_reaction("\U000025b6")

    @tasks.loop(hours=24)
    async def update_daily_leaderboard(self): # Updates the daily leaderboard message in the daily leaderboard channel.
        if self.connected:
            if self.daily_leaderboard_channel is None: return # So first we make sure that the daily_leaderboard_channel has been set.
            msg_sent = None
            for msg in await self.bot.get_channel(int(self.daily_leaderboard_channel)).history().flatten():
                if msg.author == self.bot.user and len(msg.embeds) > 0 and msg.embeds[0].title == 'Most active users today':
                    msg_sent = msg
            leaderboard = list(self.collection.find({}).sort('daily_messages_sent', pymongo.DESCENDING)) # Only going to return the top 10 results
            if len(leaderboard) == 0: return
            i = 0
            to_desc = ""
            limit = 10
            offset = 1
            while i < limit:
                try:
                    to_desc += str(i+offset) + '. ' + self.bot.get_user(leaderboard[i]['_id']).name + ':black_small_square::writing_hand:' + str(leaderboard[i]['daily_messages_sent']) + '\n'
                except(AttributeError): # Attribute error is thrown if the member, that is currently being iterated, is stored in the database but cannot be found
                    limit+=1 # So we increase the limit by 1 so that it can get enough (10 people) on to the leaderboard.
                    offset-=1 # And decrease the offset by 1 so that we have accurate position placements.
                except(IndexError): # If there is an index error then that means that there weren't more than 10 valid members for the leaderboard, so we just break and use the current leaderboard.
                    break
                i+=1
            embed = discord.Embed(title = 'Most active users today', description=to_desc)
            embed.set_footer(text = str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")))
            embed.set_thumbnail(url=self.bot.guilds[0].icon_url)
            channel = self.bot.get_channel(int(self.daily_leaderboard_channel.replace('<', '').replace('>', '').replace('#', '')))
            if msg_sent == None: await channel.send(embed=embed)
            else: await msg_sent.edit(embed=embed)
            await self.reset_daily_messages()

    @commands.command(name='monthly_leaderboard')
    async def monthly_leaderboard(self, ctx, *args):
        if self.connected:
            leaderboard = list(self.collection.find({}).sort('monthly_messages_sent', pymongo.DESCENDING))
            total_count = len(leaderboard)
            embed_list = []
            first_current_datetime = datetime.now()
            msg_sent = await ctx.send('Gathering data...')
            i = 0
            while i < total_count - 1: # Keep making pages as long as there are user entries left, just like the normal/default leaderboard.
                embed = discord.Embed(title=f'Monthly Leaderboard (Showing {i + 1} - {total_count})', description='')
                current_embed_amount = 0
                to_desc = ""
                while (current_embed_amount < 10 and i < total_count - 1):
                    to_desc += str(i+1) + '. ' + self.bot.get_user(leaderboard[i]['_id']).name + ':black_small_square::writing_hand:' + str(leaderboard[i]['monthly_messages_sent']) + '\n'
                    i += 1
                    current_embed_amount += 1
                embed.description = to_desc
                embed.title = embed.title.split('-')[0] + ' - ' + str(embed.description.split('\n')[-2].split('.')[0]) + ')'
                embed.set_footer(text = str(first_current_datetime.strftime("%d/%m/%Y %H:%M:%S")))
                embed.set_thumbnail(url=ctx.guild.icon_url)
                embed_list.append(embed)

            if len(embed_list) > 0:
                json_data = json.load(open('data.json', 'r'))
                embed_to_json_data = []
                for e in embed_list: 
                    embed_to_json_data.append({
                        'title' : e.title,
                        'description' : e.description
                    })
                
                json_data['embed_data'][msg_sent.id] = embed_to_json_data
                with open('data.json', 'w') as f:
                    json.dump(json_data, f, indent=4)
                await msg_sent.edit(content=None, embed=embed_list[0])
                await msg_sent.add_reaction("\U000025c0")
                await msg_sent.add_reaction("\U000025b6")

    async def update_monthly_leaderboard(self): # Updates the monthly leaderboard message in the monthly leaderboard channel.
        if self.connected:
            if self.monthly_leaderboard_channel is None: return # So first we make sure that the monthly_leaderboard_channel has been set.
            msg_sent = None
            for msg in await self.bot.get_channel(int(self.monthly_leaderboard_channel)).history().flatten():
                if msg.author == self.bot.user and len(msg.embeds) > 0 and msg.embeds[0].title == 'Most active users this month':
                    msg_sent = msg
            leaderboard = list(self.collection.find({}).sort('monthly_messages_sent', pymongo.DESCENDING).limit(10)) # Only going to return the top 10 results
            i = 0
            to_desc = ""
            limit = 10
            offset = 1
            while i < limit:
                try:
                    to_desc += str(i+1) + '. ' + self.bot.get_user(leaderboard[i]['_id']).name + ':black_small_square::writing_hand:' + str(leaderboard[i]['monthly_messages_sent']) + '\n'
                except(AttributeError): # Attribute error is thrown if the member, that is currently being iterated, is stored in the database but cannot be found
                    limit+=1 # So we increase the limit by 1 so that it can get enough (10 people) on to the leaderboard.
                    offset-=1 # And decrease the offset by 1 so that we have accurate position placements.
                except(IndexError): # If there is an index error then that means that there weren't more than 10 valid members for the leaderboard, so we just break and use the current leaderboard.
                    break
                i+=1
            embed = discord.Embed(title = 'Most active users this month', description=to_desc)
            embed.set_footer(text = str(datetime.now().strftime("%d/%m/%Y %H:%M:%S")))
            embed.set_thumbnail(url=self.bot.guilds[0].icon_url)
            channel = self.bot.get_channel(int(self.monthly_leaderboard_channel.replace('<', '').replace('>', '').replace('#', '')))
            if msg_sent == None: await channel.send(embed=embed)
            else: await msg_sent.edit(embed=embed)
            await self.reset_monthly_messages()

    @commands.command()
    async def set_level_up_channel(self, ctx, channel, *args):
        if not self.check_perms(ctx.author): return
        try:
            channel = self.bot.get_channel(int(channel.replace('<', '').replace('>', '').replace('#', '')))
        except(Exception): # if there was an error while trying to get the channel
            await ctx.send('The channel you sent in could not be found!')
            return
        json_data = json.load(open('data.json', 'r'))
        json_data['level_up_channel'] = channel.id
        with open('data.json', 'w') as f:
            json.dump(json_data, f, indent=4)

    @commands.command()
    async def set_daily_leaderboard_channel(self, ctx, channel, *args):
        if not self.check_perms(ctx.author): return 
        try:
            channel = self.bot.get_channel(int(channel.replace('<', '').replace('>', '').replace('#', '')))
        except(Exception): # if there was an error while trying to get the channel
            await ctx.send('The channel you sent in could not be found!')
            return
        json_data = json.load(open('data.json', 'r'))
        json_data['daily_leaderboard_channel'] = channel.id
        with open('data.json', 'w') as f:
            json.dump(json_data, f, indent=4)
        await ctx.send(f'Set the daily leaderboard channel to {channel.mention}!')

    @commands.command()
    async def set_monthly_leaderboard_channel(self, ctx, channel, *args):
        if not self.check_perms(ctx.author): return 
        try:
            channel = self.bot.get_channel(int(channel.replace('<', '').replace('>', '').replace('#', '')))
        except(Exception): # If there was an error while trying to get the channel
            await ctx.send('The channel you sent in could not be found!')
            return
        json_data = json.load(open('data.json', 'r'))
        json_data['monthly_leaderboard_channel'] = channel.id
        with open('data.json', 'w') as f:
            json.dump(json_data, f, indent=4)
        await ctx.send(f'Set the monthly leaderboard channel to {channel.mention}!')

    @commands.command(name='set_background')
    async def set_background(self, ctx, *args):
        user = ctx.author
        self.update_user_in_db(user) # Checks and registers user in db incase they aren't added
        attachments = ctx.message.attachments
        if len(attachments) == 0: 
            await ctx.send("You need to send your custom background as an attachment to the command message!")
            return
        url = attachments[0].url
        if self.connected:
            self.collection.update_one({'_id' : user.id}, {'$set' : {'background' : url}})
            await ctx.send("Updated " + user.mention + "'s background.")

    @commands.command(name='reset_background')
    async def reset_background(self, ctx, *args):
        user = ctx.author
        self.update_user_in_db(user)
        if self.connected:
            self.collection.update_one({'_id' : user.id}, {'$set' : {'background' : None}})
            await ctx.send(user.mention + "'s background has been reset.")

    @commands.command(name='add_role')
    async def add_role(self, ctx, level_arg, *role_name):
        role_name = " ".join(role_name)
        try:
            level = int(level_arg)
            if level < 1:
                raise Exception
        except(Exception):
            await ctx.send('The level you passed in was invalid!')
            return
        result = get(ctx.guild.roles, name=role_name)
        if result == None:
            await ctx.send('The role name you passed in does not exist in the server!')
            return
        json_data = json.load(open('data.json', 'r'))
        json_data['roles'].append({'name' : role_name, 'level-required' : level})
        with open('data.json', 'w') as f: 
            json.dump(json_data, f, indent=4)
        await ctx.send('Role named "' + role_name + '", which is given to users as soon as they reach level ' + level_arg)

    @commands.command()
    async def remove_role(self, ctx, role_name, *args):
        json_data = json.load(open('data.json', 'r'))
        for role in json_data['roles']:
            if role['name'] == role_name:
                json_data['roles'].remove(role)
                with open('data.json', 'w') as f:
                    json.dump(json_data, f, indent=4)
                await ctx.send('Successfully removed the ' + role_name + ' role.')
                return
        await ctx.send('That role does not exist!') # If we're able to break out of the loop without returning first, then that means we could not find the role_name in any of the role objects in the roles array.

    @commands.command()
    async def rank(self, ctx):
        author = ctx.author
        rankings = self.collection.find({}).sort("points", pymongo.DESCENDING)
        num = 1
        member_dict = dict(self.collection.find_one({"_id" : author.id}))
        level = member_dict['level']
        rank = 0
        for dictionary in rankings: # For loop to get the rank of the member.
            if(dictionary["_id"] == author.id):
                rank = num
            num += 1
        entry = self.collection.find_one({'_id' : author.id})

        if not entry['background'] is None: # If their background is not null in the database. 
            
            user_agent = 'Mozilla/5.0 (Windows; U; Windows NT 5.1; en-US; rv:1.9.0.7) Gecko/2009021910 Firefox/3.0.7' # Creating a fake user agent so that we gain access to the url adn download the background temporarily.
            url = entry['background']
            headers = {'User-Agent' : user_agent} 
            request = urllib.request.Request(url, None, headers) 
            response = urllib.request.urlopen(request)
            data = response.read() 
            file_extension = entry['background'][-5:].split('.')[1]
            f = open('current.' + file_extension, 'wb')
            f.write(data)
            f.close()
            background = Image.open('current.' + file_extension).convert('RGBA')
            background = background.resize((1200, 300))
        else: 
            background_colour = (8, 11, 12, 255) 
            background = Image.new("RGBA", (1200, 300), color=background_colour)
        await author.avatar_url_as(format="png").save(fp="avatar.png")
        logo = Image.open("avatar.png").resize((300, 300))
        bigsize = (logo.size[0] * 3, logo.size[1] * 3)
        mask = Image.new("L", bigsize, 0)
        discriminator = "#" + author.discriminator
        username = author.name
        xp = member_dict["total_xp"] - self.determine_xp(level)
        print(self.determine_xp(level + 1))
        print(self.determine_xp(level))
        finalpoints = self.determine_xp(level + 1) - self.determine_xp(level)
        theme_colour = "#ff4d00ff" # #ff4d00ff is orangish
        font = "OpenSans-Regular.ttf"

        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0) + bigsize, 255)

        # Initializing fonts (font stored in local directory)
        big_font = ImageFont.FreeTypeFont(font, 100)
        medium_font = ImageFont.FreeTypeFont(font, 31)
        small_font = ImageFont.FreeTypeFont(font, 30)

        # Putting a circle over the profile picture to make the profile picture a circle.
        mask = mask.resize(logo.size, Image.ANTIALIAS)
        logo.putalpha(mask)

        draw = ImageDraw.Draw(background)
        
        # Empty Progress Bar (Gray)
        bar_offset_x = 292
        bar_offset_y = 100
        bar_offset_x_1 = 1200
        bar_offset_y_1 = bar_offset_y + 30

        # Rectangle to cover most of the bar (and then circles are added to each side of the bar to make it look like a round bar).
        draw.rectangle((bar_offset_x, bar_offset_y, bar_offset_x_1, bar_offset_y_1), fill="#727175")


        # Making rounded corners
        im = background
        rad = 153
        circle = Image.new('L', (rad * 2, rad * 2), 0)
        draw2 = ImageDraw.Draw(circle)
        draw2.ellipse((0, 0, rad * 2, rad * 2), fill=255)
        alpha = Image.new('L', im.size, 255)
        w, h = im.size
        alpha.paste(circle.crop((0, 0, rad, rad)), (0, 0))
        alpha.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
        alpha.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
        alpha.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
        alpha.convert('RGBA')
        im.putalpha(alpha)
        background = im

        # Level and rank characters
        text_size = draw.textsize(str(level), font=big_font)
        offset_x = 850 - 55 - text_size[0]
        offset_y = 180
        draw.text((offset_x + 15, offset_y - 50), str(level), font=big_font, fill=theme_colour)

        draw.text((offset_x, offset_y + 60), "LEVEL", font=small_font, fill=theme_colour)

        text_size = draw.textsize(f"#{rank}", font=big_font)
        offset_x -= text_size[0] + 60
        draw.text((offset_x - 22, offset_y - 50), f"#{rank}", font=big_font, fill="#fff")

        draw.text((offset_x, offset_y + 60), "RANK", font=small_font, fill="#fff")

        # Filling Bar
        bar_length = bar_offset_x_1 - bar_offset_x
        progress = (finalpoints - xp) * 100 / finalpoints
        progress = 100 - progress
        progress_bar_length = round(bar_length * progress / 100)
        bar_offset_x_1 = bar_offset_x + progress_bar_length


        # Progress Bar (coloured, we make a rectangle first that covers most of the area that is supposed to be highlighted.)
        draw.rectangle((bar_offset_x, bar_offset_y, bar_offset_x_1, bar_offset_y_1), fill=theme_colour)

        # XP counter

        offset_x = 680
        offset_y = bar_offset_y - 5

        # Points marker 
        draw.text((offset_x, offset_y), f"/ {finalpoints:,} XP", font=small_font, fill="#fff")
        text_size = draw.textsize(f"{xp:,}", font=small_font)
        offset_x -= text_size[0] + 8
        draw.text((offset_x, offset_y), f"{xp:,}", font=small_font, fill="#fff")

        # User name
        text_size = draw.textsize(username, font=medium_font)
        offset_x = bar_offset_x + 60
        offset_y = bar_offset_y - 50
        draw.text((offset_x, offset_y), username, font=medium_font, fill="#fff")

        # Users discriminator
        offset_x += text_size[0] + 5
        draw.text((offset_x, offset_y), discriminator, font=medium_font, fill="#fff")

        background.paste(logo, (0, 0), mask=logo)
        background.save("rankcard1.png")
        await ctx.send(file = discord.File("rankcard1.png"))

    @commands.command()
    async def import_data(self, ctx : commands.Context, message_id : int):
        message = await ctx.fetch_message(message_id)
        data = message.embeds[0].to_dict()
        guild = message.guild
        channel = message.channel
        
        for field in data['fields']:
            current_member = {} 
            for key, value in field.items():
                value : str = value
                if key == 'name':
                    name = value.split('**')[1]
                    i = name.index(' ') + 1
                    name = name[i:]
                    level = int(value.split('\\')[1].replace('🎖',''))
                    user = get(guild.members, name=name)
                    current_member['name'] = name
                    current_member['_id'] = user.id
                    current_member['level'] = level
                    continue
                if key == 'value':
                    xp = int(value.split(' ')[0])
                    split_colon = value.split(':')
                    messages_sent = int(split_colon[2].replace(' ', ''))
                    time_in_vc = int(split_colon[6].replace(' ', ''))
                    invites = int(split_colon[10].replace(' ', ''))
                    bonus_xp = int(split_colon[18].replace(' ', ''))
                    current_member['normal_xp'] = xp - bonus_xp
                    current_member['bonus_xp'] = bonus_xp
                    current_member['total_xp'] = xp
                    current_member['voice_xp'] = 0
                    current_member['time_spent_in_vc'] = time_in_vc
                    current_member['messages_sent'] = messages_sent
                    current_member['daily_messages_sent'] = 0
                    current_member['monthly_messages_sent'] = 0
                    current_member['background'] = None
                    continue
                if self.connected:
                    if self.collection.find_one({'_id' : current_member['_id']}) is None:
                        self.collection.insert_one(current_member)
                    else: self.collection.find_one_and_replace({'_id' :  current_member['_id']}, current_member)
                elif not self.connected and not self.connect_to_db():
                    await channel.send('A connection to the database has not been established, and so data cannot be imported.')
                    return   
        await channel.send(f'Data has been imported!')

    
    
def setup(client):
    client.add_cog(Levelcog(client))