from .levelcog import Levelcog


def setup(bot):
    bot.add_cog(Levelcog(bot))