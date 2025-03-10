import {Fragment, useMemo} from 'react';
import styled from '@emotion/styled';

import GuideAnchor from 'sentry/components/assistant/guideAnchor';
import ErrorBoundary from 'sentry/components/errorBoundary';
import * as Layout from 'sentry/components/layouts/thirds';
import * as SidebarSection from 'sentry/components/sidebarSection';
import {space} from 'sentry/styles/space';
import type {Event} from 'sentry/types/event';
import type {Group, TeamParticipant, UserParticipant} from 'sentry/types/group';
import type {Project} from 'sentry/types/project';
import {getConfigForIssueType} from 'sentry/utils/issueTypeConfig';
import useOrganization from 'sentry/utils/useOrganization';
import {useUser} from 'sentry/utils/useUser';
import StreamlinedActivitySection from 'sentry/views/issueDetails/streamline/sidebar/activitySection';
import {ExternalIssueList} from 'sentry/views/issueDetails/streamline/sidebar/externalIssueList';
import FirstLastSeenSection from 'sentry/views/issueDetails/streamline/sidebar/firstLastSeenSection';
import {MergedIssuesSidebarSection} from 'sentry/views/issueDetails/streamline/sidebar/mergedSidebarSection';
import {MetricIssueSidebarSection} from 'sentry/views/issueDetails/streamline/sidebar/metricIssueSidebarSection';
import PeopleSection from 'sentry/views/issueDetails/streamline/sidebar/peopleSection';
import {SimilarIssuesSidebarSection} from 'sentry/views/issueDetails/streamline/sidebar/similarIssuesSidebarSection';
import SolutionsSection from 'sentry/views/issueDetails/streamline/sidebar/solutionsSection';

type Props = {
  group: Group;
  project: Project;
  event?: Event;
};

export default function StreamlinedSidebar({group, event, project}: Props) {
  const activeUser = useUser();
  const organization = useOrganization();

  const {userParticipants, teamParticipants, viewers} = useMemo(() => {
    return {
      userParticipants: group.participants.filter(
        (p): p is UserParticipant => p.type === 'user'
      ),
      teamParticipants: group.participants.filter(
        (p): p is TeamParticipant => p.type === 'team'
      ),
      viewers: group.seenBy.filter(user => activeUser.id !== user.id),
    };
  }, [group, activeUser.id]);

  const showPeopleSection = group.participants.length > 0 || viewers.length > 0;
  const issueTypeConfig = getConfigForIssueType(group, group.project);
  const showMetricIssueSection = event?.contexts?.metric_alert?.alert_rule_id;

  return (
    <Side>
      <GuideAnchor target="issue_sidebar_releases" position="left">
        <FirstLastSeenSection group={group} />
      </GuideAnchor>
      <StyledBreak />
      {((organization.features.includes('gen-ai-features') &&
        issueTypeConfig.issueSummary.enabled &&
        !organization.hideAiFeatures) ||
        issueTypeConfig.resources) && (
        <SolutionsSection group={group} project={project} event={event} />
      )}
      {event && (
        <ErrorBoundary mini>
          <ExternalIssueList group={group} event={event} project={project} />
          <StyledBreak style={{marginBottom: space(0.5)}} />
        </ErrorBoundary>
      )}
      <StreamlinedActivitySection group={group} />
      {showPeopleSection && (
        <Fragment>
          <StyledBreak />
          <PeopleSection
            userParticipants={userParticipants}
            teamParticipants={teamParticipants}
            viewers={viewers}
          />
        </Fragment>
      )}
      {issueTypeConfig.similarIssues.enabled && (
        <Fragment>
          <StyledBreak />
          <SimilarIssuesSidebarSection />
        </Fragment>
      )}
      {issueTypeConfig.mergedIssues.enabled && (
        <Fragment>
          <StyledBreak />
          <MergedIssuesSidebarSection />
        </Fragment>
      )}
      {showMetricIssueSection && (
        <Fragment>
          <StyledBreak />
          <MetricIssueSidebarSection event={event} />
        </Fragment>
      )}
    </Side>
  );
}

const StyledBreak = styled('hr')`
  margin-top: ${space(1.5)};
  margin-bottom: ${space(1.5)};
  border-color: ${p => p.theme.border};
`;

export const SidebarSectionTitle = styled(SidebarSection.Title)`
  margin-bottom: ${space(1)};
  color: ${p => p.theme.headingColor};
`;

const Side = styled(Layout.Side)`
  position: relative;
  padding: ${space(1.5)} ${space(2)};
  @media (max-width: ${p => p.theme.breakpoints.large}) {
    border-top: 1px solid ${p => p.theme.border};
  }
`;
