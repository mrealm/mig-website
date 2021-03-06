from django.contrib import admin
from event_cal.models import CalendarEvent, EventShift, GoogleCalendar, MeetingSignIn, MeetingSignInUserData, AnnouncementBlurb,CarpoolPerson, EventPhoto,InterviewShift

class EventInLine(admin.TabularInline):
	model = EventShift
	extra = 2
	ordering = ['start_time']

class CalendarAdmin(admin.ModelAdmin):
	inlines = [EventInLine]
	#list_filter = ['position']
	
admin.site.register(CalendarEvent,CalendarAdmin)
admin.site.register(GoogleCalendar)
admin.site.register(MeetingSignIn)
admin.site.register(MeetingSignInUserData)
admin.site.register(AnnouncementBlurb)
admin.site.register(CarpoolPerson)
admin.site.register(EventPhoto)
admin.site.register(InterviewShift)
