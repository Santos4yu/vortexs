require('dotenv').config();
const { REST, Routes, SlashCommandBuilder } = require('discord.js');

const commands = [
  new SlashCommandBuilder()
    .setName('menu')
    .setDescription('Display the Vortex Active Board — today\'s top-filtered props')
    .toJSON(),
];

const rest = new REST({ version: '10' }).setToken(process.env.DISCORD_TOKEN);

(async () => {
  try {
    console.log('Registering slash commands globally (DM-compatible)...');
    await rest.put(
      Routes.applicationCommands(process.env.CLIENT_ID),
      { body: commands },
    );
    console.log('✅  /menu command registered globally.');
  } catch (err) {
    console.error('Failed to register commands:', err);
  }
})();
