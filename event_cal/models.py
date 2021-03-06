from datetime import date,timedelta
from markdown import markdown
import json

from django.core.cache import cache
from django.core.mail import EmailMessage,send_mail
from django.core.urlresolvers import reverse
from django.db import models
from django.db.models import Max,Min,Q
from django.utils import timezone
from django.utils.encoding import force_unicode
from stdimage import StdImageField
import tweepy

from event_cal.gcal_functions import get_credentials,get_authorized_http,get_service
from mig_main.models import AcademicTerm,OfficerPosition,MemberProfile,UserProfile
from requirements.models import Requirement
from migweb.settings import DEBUG, twitter_token,twitter_secret
# Create your models here.
def default_term():
    #fixes the serialization issue
    return AcademicTerm.get_current_term()
class GoogleCalendar(models.Model):
    """
    A mostly infrastructural model that contains the name and calendar ID of a google calendar used by the chapter.
    """
    name = models.CharField(max_length = 40)
    calendar_id = models.CharField(max_length=100)
    display_order = models.PositiveSmallIntegerField(default=0)
    def __unicode__(self):
        return self.name

    
class CalendarEvent(models.Model):
    """
    An event on the TBP calendar. This class captures the essential bits of the event (without tackling the details of time and place). It details what types of requirements the event can fill, who the leaders are, whether it has been completed, how to publicize it, what term it is during, whether to restrict to members only, and more as detailed by the fairly clearly named fields.
    """
    name            = models.CharField('Event Name',max_length=50)
    description     = models.TextField('Event Description')
    leaders         = models.ManyToManyField('mig_main.MemberProfile',
                                             related_name ="event_leader")
    event_type      = models.ForeignKey('requirements.EventCategory')
    assoc_officer   = models.ForeignKey('mig_main.OfficerPosition')
    announce_text   = models.TextField('Announcement Text')
    announce_start  = models.DateField('Date to start including in announcements',
                                        default=date.today)
    completed       = models.BooleanField('Event and report completed?',default=False)
    google_cal      = models.ForeignKey(GoogleCalendar)
    project_report  = models.ForeignKey('history.ProjectReport',null=True,blank=True,
                                        on_delete = models.SET_NULL)
    term            = models.ForeignKey('mig_main.AcademicTerm', default=default_term)    
    preferred_items     = models.TextField('List any (nonobvious) items that attendees should bring, they will be prompted to see if they can.',null=True,blank=True)
    members_only    = models.BooleanField(default=True)
    needs_carpool   = models.BooleanField(default=False)
    use_sign_in     = models.BooleanField(default=False)
    allow_advance_sign_up = models.BooleanField(default=True)
    needs_facebook_event = models.BooleanField(default=False)
    needs_flyer = models.BooleanField(default=False)
    requires_UM_background_check = models.BooleanField(default=False)
    requires_AAPS_background_check = models.BooleanField(default=False)
    mutually_exclusive_shifts = models.BooleanField(default=False)
    allow_overlapping_sign_ups     = models.BooleanField(default=False)
    min_unsign_up_notice = models.PositiveSmallIntegerField('Block unsign-up how many hours before event starts?',default=12)
    min_sign_up_notice = models.PositiveSmallIntegerField('Block sign-up how many hours before event starts?',default=0)
    before_grace = timedelta(minutes=-30)
    after_grace = timedelta(hours = 1)
    @classmethod
    def get_current_term_events_alph(cls):
        """
        Gets the current term events ordered alphabetically.
        """
        return cls.objects.filter(term=AcademicTerm.get_current_term()).order_by('name')
    @classmethod
    def get_current_term_events_rchron(cls):
        """
        Gets the current term events ordered reverse-chronologically.
        """
        return cls.objects.filter(term=AcademicTerm.get_current_term()).annotate(earliest_shift=Min('eventshift__start_time')).order_by('earliest_shift')
    @classmethod
    def get_pending_events(cls):
        """
        Finds all events where all the shifts have passed but the event is not marked as completed yet.
        """
        now = timezone.localtime(timezone.now())
        return cls.objects.annotate(latest_shift=Max('eventshift__end_time')).filter(latest_shift__lte=now,completed=False)
    @classmethod
    def get_events_w_o_reports(cls,term):
        """
        Finds all events for the given term that have been marked as completed but for which there is no project report.
        """
        events = cls.objects.filter(term=term,project_report=None,completed=True)
        return events
    @classmethod
    def get_current_meeting_query(cls):
        """
        Gets the query that will find meetings currently happening.
        """
        now = timezone.localtime(timezone.now())
        return Q(use_sign_in=True)&Q(eventshift__end_time__gte=(now-cls.after_grace))&Q(eventshift__start_time__lte=(now-cls.before_grace))
    @classmethod
    def get_upcoming_events(cls,reset=False):
        """
        Returns all events that are upcoming or are happening now and have sign-in enabled.
        """
        upcoming_events = cache.get('upcoming_events',None)
        if upcoming_events and not reset:
            return upcoming_events
        now = timezone.localtime(timezone.now())
        today=date.today()
        non_meeting_query = Q(eventshift__start_time__gte=now)&Q(announce_start__lte=today)
        meeting_query = cls.get_current_meeting_query()
        not_officer_meeting = ~Q(event_type__name='Officer Meetings')
        upcoming_events = cls.objects.filter((non_meeting_query|meeting_query)&not_officer_meeting).distinct().annotate(earliest_shift=Min('eventshift__start_time')).order_by('earliest_shift')
        cache.set('upcoming_events',upcoming_events)
        return upcoming_events
    def save(self, *args, **kwargs):     
        super(CalendarEvent, self).save(*args, **kwargs) # Call the "real" save() method.
        cache.delete('EVENT_AJAX'+unicode(self.id))
    def delete(self, *args, **kwargs):     
        cache.delete('EVENT_AJAX'+unicode(self.id))
        super(CalendarEvent, self).delete(*args, **kwargs) # Call the "real" delete() method.
    def __unicode__(self):
        """
        For use in the admin or in times the event is interpreted as a string.
        """
        return self.name
    def get_relevant_event_type(self,status,standing):
        """
        For a given status and standing, this determines the type of requirement that will be filled by attending this event. Since requirements are assumed to be hierarchical, this essentially traverses upward until it finds an event cateogry listed in the requirements associated with that status and standing. If it finds none it assumes that this event won't fill any events and so the base event category is used.
        """
        requirements = Requirement.objects.filter(distinction_type__status_type__name=status,distinction_type__standing_type__name=standing,term=self.term.semester_type)
        event_category=self.event_type
        while(not requirements.filter(event_category=event_category).exists() and  event_category.parent_category):
            event_category=event_category.parent_category
        return event_category

    def get_relevant_active_event_type(self):
        """
        Convenience function for allowing event type determination in templates. Assumes active reqs are the same regardless of standing.
        """
        return self.get_relevant_event_type('Active','Undergraduate')

    def get_relevant_ugrad_electee_event_type(self):
        """
        Convenience function for allowing event type determination in templates.
        """
        return self.get_relevant_event_type('Electee','Undergraduate')

    def get_relevant_grad_electee_event_type(self):
        """
        Convenience function for allowing event type determination in templates.
        """
        return self.get_relevant_event_type('Electee','Graduate')

    def is_event_type(self,type_name):
        """
        Determines if the event satisfies a particular type of event category requirement.
        """
        is_event_type = self.event_type.name == type_name
        event_category = self.event_type
        while (not is_event_type and event_category.parent_category !=None):
            event_category = event_category.parent_category
            is_event_type = is_event_type or event_category.name ==type_name
        return is_event_type

    def is_meeting(self):
        """
        Convenience function for allowing event type determination in templates.
        """
        return self.is_event_type('Meeting Attendance')
    def is_fixed_progress(self):
        """
        Determine if an event is fixed progress (e.g. 1 social or meeting credit regardless of event length).
        """
        if self.is_meeting():
            return True
        if self.is_event_type('Social Credits'):
            return True
        return False
    def get_locations(self):
        """
        Gets a list of unique locations for the event (determined by the associated event shifts).
        """
        locations = []
        for shift in self.eventshift_set.all():
            if shift.location in locations:
                continue
            locations.append(shift.location)
        return locations
    def get_start_and_end(self):
        """
        Convenience method for determining the start and end times of the event.
        """

        output = {"start":None,'end':None}
        queryset=CalendarEvent.objects.filter(id=self.id)
        e=queryset.annotate(start_time=Min('eventshift__start_time')).annotate(end_time=Max('eventshift__end_time'))
        output['start']=e[0].start_time
        output['end']=e[0].end_time
        return output
    
    def get_attendees_with_progress(self):
        """
        Gets all the attendees (unique) who have received progress for this event.
        """
        return list(set([pr.member for pr in self.progressitem_set.all()]))
    
    def get_max_duration(self):
        """
        Calculates the maximum time that could be spent at the event by accounting for overlapping shifts or gaps in shifts.
        """
        shifts = self.eventshift_set.all().order_by('start_time')
        duration= shifts[0].end_time-shifts[0].start_time
        if shifts.count()==1:
            return duration
        previous_shift = shifts[0]
        end_time = previous_shift.end_time
        start_time = previous_shift.start_time
        duration=timedelta(hours=0)
        for shift in shifts[1:]:
            if shift.start_time<end_time:
                end_time = max(shift.end_time,end_time)
            else:
                duration+=end_time-start_time
                end_time = shift.end_time
                start_time = shift.start_time
        duration+=end_time-start_time
        return duration
    def get_event_attendees(self):
        return UserProfile.objects.filter(event_attendee__event=self).distinct()
    def get_users_who_can_bring_items(self):
        return UserProfile.objects.filter(usercanbringpreferreditem__in=self.usercanbringpreferreditem_set.filter(can_bring_item=True)).distinct()
    def get_users_who_cannot_bring_items(self):
        return UserProfile.objects.filter(usercanbringpreferreditem__in=self.usercanbringpreferreditem_set.filter(can_bring_item=False)).distinct()
    def get_attendee_hours_at_event(self,profile):
        """
        Determines how many hours the attendee spent at the event by summing the time of all shifts accounting for shifts taht are overlapped.
        """
        shifts=self.eventshift_set.filter(attendees=profile).order_by('start_time') 
        n=shifts.count()
        count = 0
        hours=0
        if not shifts.exists():
            return 0
        if self.is_fixed_progress():
            return 1
        while count< n:
            start_time = shifts[count].start_time
            end_time = shifts[count].end_time
            while count<(n-1) and shifts[count+1].start_time<end_time:
                count+=1
                end_time=shifts[count].end_time
            hours+=(end_time-start_time).seconds/3600.0
            count+=1
        return hours
    def tweet_event(self,include_hashtag):
        if self.members_only or DEBUG:
            return None
        f = open('/srv/www/twitter.dat','r')
        token = json.load(f)
        f.close()
        auth = tweepy.OAuthHandler(twitter_token,twitter_secret)
        auth.set_access_token(token[0],token[1])
        api = tweepy.API(auth)
        start_time = self.get_start_and_end()['start']
        if start_time.minute:
            disp_time = start_time.strftime('%b %d, %I:%M%p')
        else:
            disp_time = start_time.strftime('%b %d, %I%p')
        if include_hashtag:
            hashtag='\n#UmichEngin'
        else:
            hashtag=''
        max_name_length = 140-len(disp_time)-25-len(hashtag)-3
        name=self.name
        if len(name)>max_name_length:
            name = name[:(max_name_length-3)]+'...'
        tweet_text = "%(name)s:\n%(time)s\n%(link)s%(hashtag)s"%{'name':name,'time':disp_time,'link':'https://tbp.engin.umich.edu'+reverse('event_cal:event_detail',args=(self.id,)),'hashtag':hashtag }
        
        api.update_status(tweet_text)
    def notify_publicity(self,needed_flyer=False,needed_facebook=False,edited=False):
        publicity_officer = OfficerPosition.objects.filter(name='Publicity Officer')
        if publicity_officer.exists():
            publicity_email = publicity_officer[0].email
            if self.needs_flyer:
                if not needed_flyer or not edited:
                    body = r'''Hello Publicity Officer,

    An event has been created that requires a flyer to be created. The event information can be found at https://tbp.engin.umich.edu%(event_link)s

    Regards,
    The Website

    Note: This is an automated email. Please do not reply to it as responses are not checked.'''%{'event_link':reverse('event_cal:event_detail',args=(self.id,))}
                    send_mail('[TBP] Event Needs Flyer.',body,'tbp.mi.g@gmail.com',[publicity_email],fail_silently=False)
                elif edited:
                    body = r'''Hello Publicity Officer,

    An event which needs a flyer was updated. The event information can be found at https://tbp.engin.umich.edu%(event_link)s
    Please ensure that all your information is up-to-date.
    
    Regards,
    The Website

    Note: This is an automated email. Please do not reply to it as responses are not checked.'''%{'event_link':reverse('event_cal:event_detail',args=(self.id,))}
                    send_mail('[TBP] Event Needing Flyer was Updated.',body,'tbp.mi.g@gmail.com',[publicity_email],fail_silently=False)
            elif needed_flyer:
                body = r'''Hello Publicity Officer,

    An event needing a flyer is no longer listed as needing a flyer. The event information can be found at https://tbp.engin.umich.edu%(event_link)s

    Regards,
    The Website

    Note: This is an automated email. Please do not reply to it as responses are not checked.'''%{'event_link':reverse('event_cal:event_detail',args=(self.id,))}
                send_mail('[TBP] Flyer Request Cancelled.',body,'tbp.mi.g@gmail.com',[publicity_email],fail_silently=False)
            if self.needs_facebook_event:
                if not needed_facebook or not edited:
                    body = r'''Hello Publicity Officer,

    An event has been created that requires a facebook event to be created. The event information can be found at https://tbp.engin.umich.edu%(event_link)s

    Regards,
    The Website

    Note: This is an automated email. Please do not reply to it as responses are not checked.'''%{'event_link':reverse('event_cal:event_detail',args=(self.id,))}
                
                    send_mail('[TBP] Event Needs Facebook Event.',body,'tbp.mi.g@gmail.com',[publicity_email],fail_silently=False)
                elif edited:
                    body = r'''Hello Publicity Officer,

    An event which requires a facebook event was updated. The event information can be found at https://tbp.engin.umich.edu%(event_link)s
    Please make any necessary updates to the facebook event.

    Regards,
    The Website

    Note: This is an automated email. Please do not reply to it as responses are not checked.'''%{'event_link':reverse('event_cal:event_detail',args=(self.id,))}
                
                    send_mail('[TBP] Event with Facebook Event was Updated.',body,'tbp.mi.g@gmail.com',[publicity_email],fail_silently=False)
            elif needed_facebook:
                body = r'''Hello Publicity Officer,

    An event needing a facebook event is no longer listed as needing one. The event information can be found at https://tbp.engin.umich.edu%(event_link)s

    Regards,
    The Website

    Note: This is an automated email. Please do not reply to it as responses are not checked.'''%{'event_link':reverse('event_cal:event_detail',args=(self.id,))}
                send_mail('[TBP] Facebook Event Request Cancelled.',body,'tbp.mi.g@gmail.com',[publicity_email],fail_silently=False)
    def email_participants(self,subject,body,sender):
        attendees = MemberProfile.objects.filter(event_attendee__event=self)
        recipients = [attendee.get_email() for attendee in attendees]
        ccs = [leader.get_email() for leader in self.leaders.all()]
        
        email = EmailMessage(subject,body,'tbp.mi.g@gmail.com',recipients,headers={'Reply-To':sender.get_email()},cc=ccs)
        email.send()
    def delete_gcal_event(self):
        if DEBUG:
            return
        c = get_credentials()
        h = get_authorized_http(c)
        if h:
            service = get_service(h)
            for shift in self.eventshift_set.all():
                if shift.google_event_id:
                    try:
                        service.events().delete(calendarId=self.google_cal.calendar_id,eventId=shift.google_event_id).execute()
                    except:
                        pass
    def add_event_to_gcal(self,previous_cal=None):
        if DEBUG:
            return
        c = get_credentials()
        h = get_authorized_http(c)
        if h:
            service = get_service(h)
            for shift in self.eventshift_set.all():
                new_event = True
                if shift.google_event_id:
                    try:
                        if previous_cal and not (previous_cal == self.google_cal):
                            service.events().move(calendarId=previous_cal.calendar_id, eventId=shift.google_event_id, destination=self.google_cal.calendar_id).execute()
                        gcal_event = service.events().get(calendarId=self.google_cal.calendar_id,eventId=shift.google_event_id).execute()
                        if gcal_event['status']=='cancelled':
                            gcal_event={}
                            new_event = True
                        else:
                            gcal_event['sequence']=gcal_event['sequence']+1
                            new_event = False
                    except:
                        gcal_event = {}
                else:
                    gcal_event = {}
                gcal_event['summary']=self.name
                gcal_event['location']=shift.location
                gcal_event['start']={'dateTime':shift.start_time.isoformat('T'),'timeZone':'America/Detroit'}
                gcal_event['end']={'dateTime':shift.end_time.isoformat('T'),'timeZone':'America/Detroit'}
                gcal_event['recurrence']=[]
                gcal_event['description']=markdown(force_unicode(self.description),['nl2br'],safe_mode=True,enable_attributes=False)
                if not new_event :
                    service.events().update(calendarId=self.google_cal.calendar_id,eventId=shift.google_event_id,body=gcal_event).execute()
                else:
                    submitted_event=service.events().insert(calendarId=self.google_cal.calendar_id,body=gcal_event).execute()
                    shift.google_event_id=submitted_event['id']
                    shift.save()
            
    def can_complete_event(self):
        s = self.eventshift_set
        now = timezone.now()
        s_future = s.filter(end_time__gte=now)
        if self.completed:
            return False
        if s_future:
            return False
        else:
            return True
    def does_shift_overlap_with_users_other_shifts(self,shift,profile):
        attendee_shifts = self.eventshift_set.filter(attendees__in=[profile]).distinct()
        overlapping_query=Q(start_time__lt=shift.start_time,end_time__lte=shift.end_time)|Q(start_time__gte=shift.end_time,end_time__gt=shift.end_time)
        overlapping_shifts = attendee_shifts.exclude(overlapping_query)
        return overlapping_shifts.exists()
        
    def get_fullness(self):
        shifts = self.eventshift_set.all()
        if shifts.count()>1:
            return ''
        shift = shifts[0]
        num_attendees = 1.0*shift.attendees.count()
        if shift.max_attendance is None:
            if num_attendees<5:
                return '(Almost empty)'
            else:
                return '(Not full)'
        elif shift.max_attendance:
            if (num_attendees)/shift.max_attendance >.8 or shift.max_attendance-num_attendees<=1:
                return '(Nearly full)'
            elif (num_attendees)/shift.max_attendance <.2:
                return '(Almost empty)'
            else:
                return '(Not full)'
        else:
            return ''
        
class EventShift(models.Model):
    event = models.ForeignKey(CalendarEvent)
    start_time      = models.DateTimeField()
    end_time        = models.DateTimeField()
    location        = models.CharField(max_length=100,blank=True,null=True)
    on_campus       = models.BooleanField(default=False)
    google_event_id = models.CharField('ID for gcal event',max_length=64)       
    max_attendance  = models.IntegerField(null=True,blank=True,default=None)
    drivers         = models.ManyToManyField('mig_main.UserProfile',
                                             related_name ="event_driver",
                                             blank=True, null=True,default=None)
    attendees       = models.ManyToManyField('mig_main.UserProfile',
                                             related_name ="event_attendee", 
                                             blank=True, null=True,default=None)
    electees_only   = models.BooleanField(default=False)
    actives_only   = models.BooleanField(default=False)
    grads_only   = models.BooleanField(default=False)
    ugrads_only   = models.BooleanField(default=False)

    def __unicode__(self):
        return self.event.name +' shift from '+str(self.start_time)+'--'+str(self.end_time)
    def save(self, *args, **kwargs):     
        super(EventShift, self).save(*args, **kwargs) # Call the "real" save() method.
        cache.delete('EVENT_AJAX'+unicode(self.event.id))
    def delete(self, *args, **kwargs):     
        cache.delete('EVENT_AJAX'+unicode(self.event.id))
        super(EventShift, self).delete(*args, **kwargs) # Call the "real" delete() method.
    def is_full(self):
        if self.max_attendance is None:
            return False
        if self.attendees.count() >= self.max_attendance:
            return True
        return False

    def is_now(self):
        now=timezone.now()
        if now> (self.start_time+self.event.before_grace):
            if now < (self.end_time+self.event.after_grace):
                return True
        return False
    def is_before_start(self):
        now = timezone.now()
        if now > self.start_time:
            return False
        return True
    def can_sign_in(self):
        if self.event.use_sign_in and not self.is_full() and self.is_now():
            return True
        return False
    def can_sign_up(self):
        if self.event.allow_advance_sign_up and not self.is_full() and self.is_before_start():
            return True
        return False
    def get_restrictions(self):
        res_string = ''
        if self.ugrads_only:
            res_string += 'Undergrad'
        if self.grads_only:
            res_string +='Grad'
        if self.electees_only:
            res_string +=' Electee'
        if self.actives_only:
            res_string+=' Active'
        if not res_string:
            return None
        return res_string.lstrip()+'s'
    def delete_gcal_event_shift(self):
        if DEBUG:
            return
        c = get_credentials()
        h = get_authorized_http(c)
        if h:
            event=self.event
            service = get_service(h)
            if self.google_event_id:
                try:
                    service.events().delete(calendarId=event.google_cal.calendar_id,eventId=self.google_event_id).execute()
                except:
                    pass
    def add_attendee_to_gcal(self,name,email):
        if DEBUG:
            return
        c = get_credentials()
        h = get_authorized_http(c)
        if h:
            service = get_service(h)
            event=self.event
            if not self.google_event_id:
                return
            gcal_event = service.events().get(calendarId=event.google_cal.calendar_id,eventId=self.google_event_id).execute()
            if gcal_event['status']=='cancelled':
                return
            else:
                gcal_event['sequence']+=1
                if 'attendees' in gcal_event:
                    gcal_event['attendees'].append(
                        {
                            'email':email,
                            'displayName':name,
                        })
                else:
                    gcal_event['attendees']=[{
                            'email':email,
                            'displayName':name,
                        }]
                    
                service.events().update(calendarId=event.google_cal.calendar_id,eventId=self.google_event_id,body=gcal_event).execute()
                
    def delete_gcal_attendee(self,email):
        if DEBUG:
            return
        c = get_credentials()
        h = get_authorized_http(c)
        if h:
            service = get_service(h)
            event=self.event
            if not self.google_event_id:
                return
            try:
                gcal_event = service.events().get(calendarId=event.google_cal.calendar_id,eventId=self.google_event_id).execute()
                if gcal_event['status']=='cancelled':
                    return
                else:
                    gcal_event['sequence']+=1
                    if 'attendees' in gcal_event:
                        gcal_event['attendees'][:]=[a for a in gcal_event['attendees'] if a.get('email') !=email]
                        service.events().update(calendarId=event.google_cal.calendar_id,eventId=self.google_event_id,body=gcal_event).execute()
            except:
                return   
    def get_ordered_waitlist(self):
        return self.waitlistslot_set.all().order_by('time_added')
    def get_waitlist_length(self):
        return self.waitlistslot_set.count()
    def get_users_waitlist_spot(self,profile):
        user_waitlist=WaitlistSlot.objects.filter(shift=self,user=profile)
        if not user_waitlist:
            return self.get_waitlist_length()
        return WaitlistSlot.objects.filter(shift=self,time_added__lt=user_waitlist[0].time_added).count()
    def get_users_on_waitlist(self):
        return UserProfile.objects.filter(waitlistslot__in=self.waitlistslot_set.all())
class InterviewShift(models.Model):
    interviewer_shift = models.ForeignKey(EventShift,related_name='shift_interviewer')
    interviewee_shift = models.ForeignKey(EventShift,related_name='shift_interviewee')
    term = models.ForeignKey('mig_main.AcademicTerm')
    def user_can_followup(interview,user):
        if not hasattr(user,'userprofile') or not user.userprofile.is_member():
            return False
        if not user.userprofile in interview.interviewer_shift.attendees.all():
            return False
        if interview.interviewer_shift.start_time>=timezone.now():
            return False
        return True
class MeetingSignIn(models.Model):
    event = models.ForeignKey(CalendarEvent)
    code_phrase = models.CharField(max_length=100)
    quick_question = models.TextField()


class MeetingSignInUserData(models.Model):
    meeting_data = models.ForeignKey(MeetingSignIn)
    question_response = models.TextField()
    free_response = models.TextField()

class AnnouncementBlurb(models.Model):
    start_date = models.DateField()
    end_date = models.DateField()
    title = models.CharField(max_length = 150)
    text = models.TextField()
    contacts = models.ManyToManyField('mig_main.UserProfile')
    sign_up_link = models.CharField(max_length = 128,blank=True,null=True)
    def __unicode__(self):
        return self.title

class EventPhoto(models.Model):
    event = models.ForeignKey(CalendarEvent,blank=True,null=True)
    project_report = models.ForeignKey('history.ProjectReport',blank=True,null=True)
    caption = models.TextField(blank=True,null=True)
    photo   = StdImageField(upload_to='event_photos',variations={'thumbnail':(800,800)})

class CarpoolPerson(models.Model):
    event = models.ForeignKey(CalendarEvent)
    person = models.ForeignKey('mig_main.UserProfile')
    ROLES_CHOICES = (
            ('DR','Driver'),
            ('RI','Rider'),
    )
    LOCATION_CHOICES = (
            ('NC','North Campus'),
            ('CC','Central Campus'),
            ('SC','South Campus'),
    )
    role = models.CharField(max_length=2,
                            choices=ROLES_CHOICES,
                            default='RI')
    location = models.CharField('Which location is closest to you?',max_length=2,
                            choices=LOCATION_CHOICES,
                            default='CC')
    number_seats = models.PositiveSmallIntegerField(null=True,blank=True)

class WaitlistSlot(models.Model):
    shift = models.ForeignKey(EventShift)
    time_added = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey('mig_main.UserProfile')

class InterviewPairing(models.Model):
    first_shift = models.ForeignKey(EventShift,related_name='pairing_first')
    second_shift = models.ForeignKey(EventShift,related_name='pairing_second')

class UserCanBringPreferredItem(models.Model):
    event = models.ForeignKey(CalendarEvent)
    user = models.ForeignKey('mig_main.UserProfile')
    can_bring_item = models.BooleanField('Yes, I can bring the item. ',default=False)
